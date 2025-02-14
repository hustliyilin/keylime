import base64
import binascii
import hashlib
import os
import re
import sys
import tempfile
import threading
import typing
import zlib
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from cryptography.hazmat.primitives import serialization as crypto_serialization
from packaging.version import Version

from keylime import cert_utils, cmd_exec, config, json, keylime_logging, measured_boot
from keylime.agentstates import AgentAttestState, TPMClockInfo
from keylime.common import algorithms
from keylime.common.algorithms import Hash
from keylime.elchecking.policies import RefState
from keylime.failure import Component, Failure
from keylime.ima import ima
from keylime.ima.file_signatures import ImaKeyrings
from keylime.ima.types import RuntimePolicyType
from keylime.tpm import tpm2_objects, tpm_abstract, tpm_util

logger = keylime_logging.init_logging("tpm")

EXIT_SUCCESS: int = 0


class Tpm:
    tools_version: str = ""

    tpmutilLock: threading.Lock

    def __init__(self) -> None:
        super().__init__()
        # Shared lock to serialize access to tools
        self.tpmutilLock = threading.Lock()

        self.__get_tpm2_tools()

    def __get_tpm2_tools(self) -> None:
        retDict = self.__run(["tpm2_startup", "--version"])

        code = retDict["code"]
        output = "".join(config.convert(retDict["retout"]))
        errout = "".join(config.convert(retDict["reterr"]))
        if code != EXIT_SUCCESS:
            raise Exception(
                "Error establishing tpm2-tools version using TPM2_Startup: %s" + str(code) + ": " + str(errout)
            )

        # Extract the `version="x.x.x"` from tools
        version_str_ = re.search(r'version="([^"]+)"', output)
        if version_str_ is None:
            msg = f"Could not determine tpm2-tools version from TPM2_Startup output '{output}'"
            logger.error(msg)
            raise Exception(msg)
        version_str = version_str_.group(1)
        # Extract the full semver release number.
        tools_version = version_str.split("-")

        if Version(tools_version[0]) >= Version("5.4") or (
            # Also mark first git version that introduces the change to the tpm2_eventlog format as 5.4
            # See: https://github.com/tpm2-software/tpm2-tools/commit/c78d258b2588aee535fd17594ad2f5e808056373
            Version(tools_version[0]) == Version("5.3")
            and len(tools_version) > 1
            and int(tools_version[1]) >= 24
        ):
            logger.info("TPM2-TOOLS Version: %s", tools_version[0])
            self.tools_version = "5.4"
        elif Version(tools_version[0]) >= Version("4.2"):
            logger.info("TPM2-TOOLS Version: %s", tools_version[0])
            self.tools_version = "4.2"
        elif Version(tools_version[0]) >= Version("4.0.0"):
            logger.info("TPM2-TOOLS Version: %s", tools_version[0])
            self.tools_version = "4.0"
        elif Version(tools_version[0]) >= Version("3.2.0"):
            logger.info("TPM2-TOOLS Version: %s", tools_version[0])
            self.tools_version = "3.2"
        else:
            logger.error("TPM2-TOOLS Version %s is not supported.", tools_version[0])
            sys.exit()

    def __run(
        self,
        cmd: Sequence[str],
        lock: bool = True,
    ) -> cmd_exec.RetDictType:
        if lock:
            with self.tpmutilLock:
                retDict = cmd_exec.run(cmd=cmd, expectedcode=EXIT_SUCCESS, raiseOnError=False)
        else:
            retDict = cmd_exec.run(cmd=cmd, expectedcode=EXIT_SUCCESS, raiseOnError=False)
        code = retDict["code"]
        retout = retDict["retout"]
        reterr = retDict["reterr"]

        # Don't bother continuing if TPM call failed and we're raising on error
        if code != EXIT_SUCCESS:
            raise Exception(
                f"Command: {cmd} returned {code}, expected {EXIT_SUCCESS}, output {retout}, stderr {reterr}"
            )

        return retDict

    def encryptAIK(self, uuid: str, ek_tpm: bytes, aik_tpm: bytes) -> Optional[Tuple[bytes, str]]:
        if ek_tpm is None or aik_tpm is None:
            logger.error("Missing parameters for encryptAIK")
            return None

        aik_name = tpm2_objects.get_tpm2b_public_name(aik_tpm)

        efd = keyfd = blobfd = -1
        ekFile = None
        challengeFile = None
        keyblob = None
        blobpath = None

        try:
            # write out the public EK
            efd, etemp = tempfile.mkstemp()
            with open(etemp, "wb") as ekFile:
                ekFile.write(ek_tpm)

            # write out the challenge
            challenge_str = tpm_abstract.TPM_Utilities.random_password(32)
            challenge = challenge_str.encode()
            keyfd, keypath = tempfile.mkstemp()
            with open(keypath, "wb") as challengeFile:
                challengeFile.write(challenge)

            # create temp file for the blob
            blobfd, blobpath = tempfile.mkstemp()
            command = [
                "tpm2_makecredential",
                "-T",
                "none",
                "-e",
                ekFile.name,
                "-s",
                challengeFile.name,
                "-n",
                aik_name,
                "-o",
                blobpath,
            ]
            self.__run(command, lock=False)

            logger.info("Encrypting AIK for UUID %s", uuid)

            # read in the blob
            with open(blobpath, "rb") as f:
                keyblob = base64.b64encode(f.read())

            # read in the aes key
            key = base64.b64encode(challenge).decode("utf-8")

        except Exception as e:
            logger.error("Error encrypting AIK: %s", str(e))
            logger.exception(e)
            raise
        finally:
            for fd in [efd, keyfd, blobfd]:
                if fd >= 0:
                    os.close(fd)
            for fi in [ekFile, challengeFile]:
                if fi is not None:
                    os.remove(fi.name)
            if blobpath is not None:
                os.remove(blobpath)

        return (keyblob, key)

    @staticmethod
    def verify_ek(ekcert: bytes, tpm_cert_store: str) -> bool:
        """Verify that the provided EK certificate is signed by a trusted root
        :param ekcert: The Endorsement Key certificate in DER format
        :returns: True if the certificate can be verified, false otherwise
        """
        return cert_utils.verify_ek(ekcert, tpm_cert_store)

    @staticmethod
    def _tpm2_clock_info_from_quote(quote: str, compressed: bool) -> Dict[str, Any]:
        """Get TPM timestamp info from quote
        :param quote: quote data in the format 'r<b64-compressed-quoteblob>:<b64-compressed-sigblob>:<b64-compressed-pcrblob>
        :param compressed: if the quote data is compressed with zlib or not
        :returns: Returns a dict holding the TPMS_CLOCK_INFO fields
        This function throws an Exception on bad input.
        """

        if quote[0] != "r":
            raise Exception(f"Invalid quote type {quote[0]}")
        quote = quote[1:]

        quote_tokens = quote.split(":")
        if len(quote_tokens) < 3:
            raise Exception(f"Quote is not compound! {quote}")

        quoteblob = base64.b64decode(quote_tokens[0])

        if compressed:
            logger.warning("Decompressing quote data which is unsafe!")
            quoteblob = zlib.decompress(quoteblob)

        try:
            return tpm2_objects.get_tpms_attest_clock_info(quoteblob)
        except Exception as e:
            logger.error("Error extracting clock info from quote: %s", str(e))
            logger.exception(e)
            return {}

    @staticmethod
    def _tpm2_checkquote(
        aikTpmFromRegistrar: str, quote: str, nonce: str, hash_alg: str, compressed: bool
    ) -> Tuple[Dict[int, str], str]:
        """Write the files from data returned from tpm2_quote for running tpm2_checkquote
        :param aikTpmFromRegistrar: AIK used to generate the quote and is needed for verifying it now.
        :param quote: quote data in the format 'r<b64-compressed-quoteblob>:<b64-compressed-sigblob>:<b64-compressed-pcrblob>
        :param nonce: nonce that was used to create the quote
        :param hash_alg: the hash algorithm that was used
        :param compressed: if the quote data is compressed with zlib or not
        :returns: Returns the 'retout' from running tpm2_checkquote and True in case of success, None and False in case of error.
        This function throws an Exception on bad input.
        """
        aikFromRegistrar = tpm2_objects.pubkey_from_tpm2b_public(
            base64.b64decode(aikTpmFromRegistrar),
        ).public_bytes(
            crypto_serialization.Encoding.PEM,
            crypto_serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        if quote[0] != "r":
            raise Exception(f"Invalid quote type {quote[0]}")
        quote = quote[1:]

        quote_tokens = quote.split(":")
        if len(quote_tokens) < 3:
            raise Exception(f"Quote is not compound! {quote}")

        quoteblob = base64.b64decode(quote_tokens[0])
        sigblob = base64.b64decode(quote_tokens[1])
        pcrblob = base64.b64decode(quote_tokens[2])

        if compressed:
            logger.warning("Decompressing quote data which is unsafe!")
            quoteblob = zlib.decompress(quoteblob)
            sigblob = zlib.decompress(sigblob)
            pcrblob = zlib.decompress(pcrblob)

        try:
            pcrs_dict = tpm_util.checkquote(aikFromRegistrar, nonce, sigblob, quoteblob, pcrblob, hash_alg)
        except Exception as e:
            logger.error("Error verifying quote: %s", str(e))
            logger.exception(e)
            return {}, str(e)

        return pcrs_dict, ""

    @staticmethod
    def __check_ima(
        agentAttestState: AgentAttestState,
        pcrval: str,
        ima_measurement_list: str,
        runtime_policy: Optional[RuntimePolicyType],
        ima_keyrings: Optional[ImaKeyrings],
        boot_aggregates: Optional[Dict[str, List[str]]],
        hash_alg: Hash,
    ) -> Failure:
        failure = Failure(Component.IMA)
        logger.info("Checking IMA measurement list on agent: %s", agentAttestState.get_agent_id())
        _, ima_failure = ima.process_measurement_list(
            agentAttestState,
            ima_measurement_list.split("\n"),
            runtime_policy,
            pcrval=pcrval,
            ima_keyrings=ima_keyrings,
            boot_aggregates=boot_aggregates,
            hash_alg=hash_alg,
        )
        failure.merge(ima_failure)
        if not failure:
            logger.debug("IMA measurement list of agent %s validated", agentAttestState.get_agent_id())
        return failure

    def check_pcrs(
        self,
        agentAttestState: AgentAttestState,
        tpm_policy: Union[str, Dict[str, Any]],
        pcrs_dict: Dict[int, str],
        data: str,
        ima_measurement_list: Optional[str],
        runtime_policy: Optional[RuntimePolicyType],
        ima_keyrings: Optional[ImaKeyrings],
        mb_measurement_list: Optional[str],
        mb_refstate_str: Optional[str],
        hash_alg: Hash,
    ) -> Failure:
        failure = Failure(Component.PCR_VALIDATION)

        agent_id = agentAttestState.get_agent_id()

        if isinstance(tpm_policy, str):
            tpm_policy_dict = json.loads(tpm_policy)
        else:
            tpm_policy_dict = tpm_policy

        pcr_allowlist = tpm_policy_dict.copy()

        if "mask" in pcr_allowlist:
            del pcr_allowlist["mask"]
        # convert all pcr num keys to integers
        pcr_allowlist = {int(k): v for k, v in list(pcr_allowlist.items())}

        mb_policy, mb_policy_name, mb_refstate_data = measured_boot.get_policy(mb_refstate_str)
        if mb_refstate_data:
            logger.debug(
                "Evaluating measured boot log sent by agent %s using a measured boot reference state containing %d entries with measured boot policy %s",
                agent_id,
                len(mb_refstate_data),
                mb_policy_name,
            )
        mb_pcrs_hashes, boot_aggregates, mb_measurement_data, mb_failure = self.parse_mb_bootlog(
            mb_measurement_list, hash_alg
        )
        failure.merge(mb_failure)

        pcrs_in_quote: Set[int] = set()  # PCRs in quote that were already used for some kind of validation

        pcr_nums = set(pcrs_dict.keys())

        # Validate data PCR
        if config.TPM_DATA_PCR in pcr_nums and data is not None:
            expectedval = self.sim_extend(data, hash_alg)
            if expectedval != pcrs_dict[config.TPM_DATA_PCR]:
                logger.error(
                    "PCR #%s: invalid bind data %s from quote (from agent %s) does not match expected value %s",
                    config.TPM_DATA_PCR,
                    pcrs_dict[config.TPM_DATA_PCR],
                    agent_id,
                    expectedval,
                )
                failure.add_event(
                    f"invalid_pcr_{config.TPM_DATA_PCR}",
                    {"got": pcrs_dict[config.TPM_DATA_PCR], "expected": expectedval},
                    True,
                )
            pcrs_in_quote.add(config.TPM_DATA_PCR)
        else:
            logger.error(
                "Binding PCR #%s was not included in the quote (from agent %s), but is required",
                config.TPM_DATA_PCR,
                agent_id,
            )
            failure.add_event(
                f"missing_pcr_{config.TPM_DATA_PCR}",
                f"Data PCR {config.TPM_DATA_PCR} is missing in quote, but is required",
                True,
            )
        # Check for ima PCR
        if config.IMA_PCR in pcr_nums:
            if ima_measurement_list is None:
                logger.error("IMA PCR in policy, but no measurement list provided by agent %s", agent_id)
                failure.add_event(
                    f"unused_pcr_{config.IMA_PCR}", "IMA PCR in policy, but no measurement list provided", True
                )
            else:
                ima_failure = Tpm.__check_ima(
                    agentAttestState,
                    pcrs_dict[config.IMA_PCR],
                    ima_measurement_list,
                    runtime_policy,
                    ima_keyrings,
                    boot_aggregates,
                    hash_alg,
                )
                failure.merge(ima_failure)

            pcrs_in_quote.add(config.IMA_PCR)

        # Collect mismatched measured boot PCRs as measured_boot failures
        mb_pcr_failure = Failure(Component.MEASURED_BOOT)
        # Handle measured boot PCRs only if the parsing worked
        if not mb_failure:
            for pcr_num in set(config.MEASUREDBOOT_PCRS) & pcr_nums:
                if mb_refstate_data:
                    if not mb_measurement_list:
                        logger.error(
                            "Measured Boot PCR %d in policy, but no measurement list provided by agent %s",
                            pcr_num,
                            agent_id,
                        )
                        failure.add_event(
                            f"unused_pcr_{pcr_num}",
                            f"Measured Boot PCR {pcr_num} in policy, but no measurement list provided",
                            True,
                        )
                        continue

                    val_from_log_int = mb_pcrs_hashes.get(str(pcr_num), 0)
                    val_from_log_hex = hex(val_from_log_int)[2:]
                    val_from_log_hex_stripped = val_from_log_hex.lstrip("0")
                    pcrval_stripped = pcrs_dict[pcr_num].lstrip("0")
                    if val_from_log_hex_stripped != pcrval_stripped:
                        logger.error(
                            "For PCR %d and hash %s the boot event log has value %r but the agent %s returned %r",
                            pcr_num,
                            str(hash_alg),
                            val_from_log_hex,
                            agent_id,
                            pcrs_dict[pcr_num],
                        )
                        mb_pcr_failure.add_event(
                            f"invalid_pcr_{pcr_num}",
                            {
                                "context": "SHA256 boot event log PCR value does not match",
                                "got": pcrs_dict[pcr_num],
                                "expected": val_from_log_hex,
                            },
                            True,
                        )

                    if pcr_num in pcr_allowlist and pcrs_dict[pcr_num] not in pcr_allowlist[pcr_num]:
                        logger.error(
                            "PCR #%s: %s from quote (from agent %s) does not match expected value %s",
                            pcr_num,
                            pcrs_dict[pcr_num],
                            agent_id,
                            pcr_allowlist[pcr_num],
                        )
                        failure.add_event(
                            f"invalid_pcr_{pcr_num}",
                            {
                                "context": "PCR value is not in allowlist",
                                "got": pcrs_dict[pcr_num],
                                "expected": pcr_allowlist[pcr_num],
                            },
                            True,
                        )
                    pcrs_in_quote.add(pcr_num)
        failure.merge(mb_pcr_failure)

        # Check the remaining non validated PCRs
        for pcr_num in pcr_nums - pcrs_in_quote:
            if pcr_num not in list(pcr_allowlist.keys()):
                logger.warning(
                    "PCR #%s in quote (from agent %s) not found in tpm_policy, skipping.",
                    pcr_num,
                    agent_id,
                )
                continue
            if pcrs_dict[pcr_num] not in pcr_allowlist[pcr_num]:
                logger.error(
                    "PCR #%s: %s from quote (from agent %s) does not match expected value %s",
                    pcr_num,
                    pcrs_dict[pcr_num],
                    agent_id,
                    pcr_allowlist[pcr_num],
                )
                failure.add_event(
                    f"invalid_pcr_{pcr_num}",
                    {
                        "context": "PCR value is not in allowlist",
                        "got": pcrs_dict[pcr_num],
                        "expected": pcr_allowlist[pcr_num],
                    },
                    True,
                )

            pcrs_in_quote.add(pcr_num)

        missing = set(pcr_allowlist.keys()) - pcrs_in_quote
        if len(missing) > 0:
            logger.error("PCRs specified in policy not in quote (from agent %s): %s", agent_id, missing)
            failure.add_event("missing_pcrs", {"context": "PCRs are missing in quote", "data": list(missing)}, True)

        if not mb_failure and mb_refstate_data:
            mb_policy_failure = measured_boot.evaluate_policy(
                mb_policy,
                mb_policy_name,
                mb_refstate_data,
                mb_measurement_data,
                pcrs_in_quote,
                agentAttestState.get_agent_id(),
            )
            failure.merge(mb_policy_failure)

        return failure

    def check_quote(
        self,
        agentAttestState: AgentAttestState,
        nonce: str,
        data: str,
        quote: str,
        aikTpmFromRegistrar: str,
        tpm_policy: Optional[Union[str, Dict[str, Any]]] = None,
        ima_measurement_list: Optional[str] = None,
        runtime_policy: Optional[RuntimePolicyType] = None,
        hash_alg: Optional[Hash] = None,
        ima_keyrings: Optional[ImaKeyrings] = None,
        mb_measurement_list: Optional[str] = None,
        mb_refstate: Optional[str] = None,
        compressed: bool = False,
    ) -> Failure:
        if tpm_policy is None:
            tpm_policy = {}

        if runtime_policy is None:
            runtime_policy = ima.EMPTY_RUNTIME_POLICY

        failure = Failure(Component.QUOTE_VALIDATION)
        if hash_alg is None:
            failure.add_event("hash_alg_missing", "Hash algorithm cannot be empty", False)
            return failure

        # First and foremost, the quote needs to be validated
        pcrs_dict, err = Tpm._tpm2_checkquote(aikTpmFromRegistrar, quote, nonce, str(hash_alg), compressed)
        if err:
            # If the quote validation fails we will skip all other steps therefore this failure is irrecoverable.
            failure.add_event("quote_validation", {"message": "Quote data validation", "error": err}, False)
            return failure

        # Only after validating the quote, the TPM clock information can be extracted from it.
        clock_failure, current_clock_info = Tpm.check_quote_timing(
            agentAttestState.get_tpm_clockinfo(), quote, compressed
        )
        if clock_failure:
            failure.add_event(
                "quote_validation",
                {"message": "Validation of clockinfo from quote using tpm2-tools", "data": clock_failure},
                False,
            )
            return failure
        if current_clock_info:
            agentAttestState.set_tpm_clockinfo(current_clock_info)

        if len(pcrs_dict) == 0:
            logger.warning(
                "Quote for agent %s does not contain any PCRs. Make sure that the TPM supports %s PCR banks",
                agentAttestState.agent_id,
                str(hash_alg),
            )

        return self.check_pcrs(
            agentAttestState,
            tpm_policy,
            pcrs_dict,
            data,
            ima_measurement_list,
            runtime_policy,
            ima_keyrings,
            mb_measurement_list,
            mb_refstate,
            hash_alg,
        )

    @staticmethod
    def check_quote_timing(
        previous_clockinfo: TPMClockInfo, quote: str, compressed: bool
    ) -> Tuple[Optional[str], Optional[TPMClockInfo]]:
        # Sanity check quote clock information

        current_clockinfo = None

        clock_info_dict = Tpm._tpm2_clock_info_from_quote(quote, compressed)
        if not clock_info_dict:
            return "_tpm2_clock_info_from_quote failed ", current_clockinfo

        tentative_current_clockinfo = TPMClockInfo.from_dict(clock_info_dict)

        resetdiff = tentative_current_clockinfo.resetcount - previous_clockinfo.resetcount
        restartdiff = tentative_current_clockinfo.restartcount - previous_clockinfo.restartcount

        if resetdiff < 0:
            return "resetCount value decreased on TPM between two consecutive quotes", current_clockinfo

        if restartdiff < 0:
            return "restartCount value decreased on TPM between two consecutive quotes", current_clockinfo

        if tentative_current_clockinfo.safe != 1:
            return "clock safe flag is disabled", current_clockinfo

        if not (resetdiff and restartdiff):
            if tentative_current_clockinfo.clock - previous_clockinfo.clock <= 0:
                return (
                    "clock timestamp did issued by TPM did not increase between two consecutive quotes",
                    current_clockinfo,
                )

            current_clockinfo = tentative_current_clockinfo

        return None, current_clockinfo

    @staticmethod
    def sim_extend(hashval_1: str, hash_alg: Hash) -> str:
        """Compute expected value  H(0|H(data))"""
        hdata = hash_alg.hash(hashval_1.encode("utf-8"))
        hext = hash_alg.hash(hash_alg.get_start_hash() + hdata)
        return hext.hex()

    @staticmethod
    def __stringify_pcr_keys(log: Dict[str, Dict[str, Dict[str, str]]]) -> None:
        """Ensure that the PCR indices are strings

        The YAML produced by `tpm2_eventlog`, when loaded by the yaml module,
        uses integer keys in the dicts holding PCR contents.  That does not
        correspond to any JSON data.  This method ensures those keys are
        strings.
        The log is untrusted because it ultimately comes from an untrusted
        source and has been processed by software that has had bugs."""
        if (not isinstance(log, dict)) or "pcrs" not in log:
            return
        old_pcrs = log["pcrs"]
        if not isinstance(old_pcrs, dict):
            return
        new_pcrs = {}
        for hash_alg, cells in old_pcrs.items():
            if not isinstance(cells, dict):
                new_pcrs[hash_alg] = cells
                continue
            new_pcrs[hash_alg] = {str(index): val for index, val in cells.items()}
        log["pcrs"] = new_pcrs
        return

    @staticmethod
    def __add_boot_aggregate(log: Dict[str, Any]) -> None:
        """Scan the boot event log and calculate possible boot aggregates.

        Hashes are calculated for both sha1 and sha256,
        as well as for 8 or 10 participant PCRs.

        Technically the sha1/10PCR combination is unnecessary, since it has no
        implementation.

        Error conditions caused by improper string formatting etc. are
        ignored. The current assumption is that the boot event log PCR
        values are in decimal encoding, but this is liable to change."""
        if (not isinstance(log, dict)) or "pcrs" not in log:
            return
        log["boot_aggregates"] = {}
        for hashalg in log["pcrs"].keys():
            log["boot_aggregates"][hashalg] = []
            for maxpcr in [8, 10]:
                try:
                    hashclass = getattr(hashlib, hashalg)
                    h = hashclass()
                    for pcrno in range(0, maxpcr):
                        pcrstrg = log["pcrs"][hashalg][str(pcrno)]
                        pcrhex = f"{pcrstrg:0{h.digest_size*2}x}"
                        h.update(bytes.fromhex(pcrhex))
                    log["boot_aggregates"][hashalg].append(h.hexdigest())
                except Exception:
                    pass

    @staticmethod
    def __unescape_eventlog(log: Dict) -> None:  # type: ignore
        """
        Newer versions of tpm2-tools escapes the YAML output and including the trailing null byte.
        See: https://github.com/tpm2-software/tpm2-tools/commit/c78d258b2588aee535fd17594ad2f5e808056373
        This converts it back to an unescaped string.
        Example:
            '"MokList\\0"' -> 'MokList'
        """
        if Tpm.tools_version in ["3.2", "4.0", "4.2"]:
            return

        escaped_chars = [
            ("\0", "\\0"),
            ("\a", "\\a"),
            ("\b", "\\b"),
            ("\t", "\\t"),
            ("\v", "\\v"),
            ("\f", "\\f"),
            ("\r", "\\r"),
            ("\x1b", "\\e"),
            ("'", "\\'"),
            ("\\", "\\\\"),
        ]

        def recursive_unescape(data):  # type: ignore
            if isinstance(data, str):
                if data.startswith('"') and data.endswith('"'):
                    data = data[1:-1]
                    for orig, escaped in escaped_chars:
                        data = data.replace(escaped, orig)
                    data = data.rstrip("\0")
            elif isinstance(data, dict):
                for key, value in data.items():
                    data[key] = recursive_unescape(value)  # type: ignore
            elif isinstance(data, list):
                for pos, item in enumerate(data):
                    data[pos] = recursive_unescape(item)  # type: ignore
            return data

        recursive_unescape(log)  # type: ignore

    def parse_binary_bootlog(self, log_bin: bytes) -> typing.Tuple[Failure, typing.Optional[Dict[str, Any]]]:
        """Parse and enrich a BIOS boot log

        The input is the binary log.
        The output is the result of parsing and applying other conveniences."""
        failure = Failure(Component.MEASURED_BOOT, ["parser"])
        with tempfile.NamedTemporaryFile() as log_bin_file:
            log_bin_file.write(log_bin)
            log_bin_file.seek(0)
            log_bin_filename = log_bin_file.name
            try:
                retDict_tpm2 = self.__run(["tpm2_eventlog", "--eventlog-version=2", log_bin_filename])
            except Exception:
                failure.add_event("tpm2_eventlog", "running tpm2_eventlog failed", True)
                return failure, None
        log_parsed_strs = retDict_tpm2["retout"]
        if len(retDict_tpm2["reterr"]) > 0:
            failure.add_event(
                "tpm2_eventlog.warning",
                {"context": "tpm2_eventlog exited with warnings", "data": str(retDict_tpm2["reterr"])},
                True,
            )
            return failure, None
        log_parsed_data = config.yaml_to_dict(log_parsed_strs, add_newlines=False, logger=logger)
        if log_parsed_data is None:
            failure.add_event("yaml", "yaml output of tpm2_eventlog could not be parsed!", True)
            return failure, None
        # pylint: disable=import-outside-toplevel
        try:
            from keylime import tpm_bootlog_enrich
        except Exception as e:
            logger.error("Could not load tpm_bootlog_enrich (which depends on %s): %s", config.LIBEFIVAR, str(e))
            failure.add_event(
                "bootlog_enrich",
                f"Could not load tpm_bootlog_enrich (which depends on {config.LIBEFIVAR}): {str(e)}",
                True,
            )
            return failure, None
        # pylint: enable=import-outside-toplevel
        tpm_bootlog_enrich.enrich(log_parsed_data)
        Tpm.__stringify_pcr_keys(log_parsed_data)
        Tpm.__add_boot_aggregate(log_parsed_data)
        Tpm.__unescape_eventlog(log_parsed_data)
        return failure, log_parsed_data

    def _parse_mb_bootlog(self, log_b64: str) -> typing.Tuple[Failure, typing.Optional[Dict[str, Any]]]:
        """Parse and enrich a BIOS boot log

        The input is the base64 encoding of a binary log.
        The output is the result of parsing and applying other conveniences."""
        failure = Failure(Component.MEASURED_BOOT, ["parser"])
        try:
            log_bin = base64.b64decode(log_b64, validate=True)
            failure_mb, result = self.parse_binary_bootlog(log_bin)
            if failure_mb:
                failure.merge(failure_mb)
                result = None
        except binascii.Error:
            failure.add_event("log.base64decode", "Measured boot log could not be decoded", True)
            result = None
        return failure, result

    def parse_mb_bootlog(
        self, mb_measurement_list: Optional[str], hash_alg: algorithms.Hash
    ) -> typing.Tuple[Dict[str, int], typing.Optional[Dict[str, List[str]]], RefState, Failure]:
        """Parse the measured boot log and return its object and the state of the PCRs
        :param mb_measurement_list: The measured boot measurement list
        :param hash_alg: the hash algorithm that should be used for the PCRs
        :returns: Returns a map of the state of the PCRs, measured boot data object and True for success
                  and False in case an error occurred
        """
        failure = Failure(Component.MEASURED_BOOT, ["parser"])
        if mb_measurement_list:
            failure_mb, mb_measurement_data = self._parse_mb_bootlog(mb_measurement_list)
            if not mb_measurement_data:
                failure.merge(failure_mb)
                logger.error("Unable to parse measured boot event log. Check previous messages for a reason for error.")
                return {}, None, {}, failure
            log_pcrs = mb_measurement_data.get("pcrs")
            if not isinstance(log_pcrs, dict):
                logger.error("Parse of measured boot event log has unexpected value for .pcrs: %r", log_pcrs)
                failure.add_event("invalid_pcrs", {"got": log_pcrs}, True)
                return {}, None, {}, failure
            pcr_hashes = log_pcrs.get(str(hash_alg))
            if (not isinstance(pcr_hashes, dict)) or not pcr_hashes:
                logger.error(
                    "Parse of measured boot event log has unexpected value for .pcrs.%s: %r", str(hash_alg), pcr_hashes
                )
                failure.add_event("invalid_pcrs_hashes", {"got": pcr_hashes}, True)
                return {}, None, {}, failure
            boot_aggregates = mb_measurement_data.get("boot_aggregates")
            if (not isinstance(boot_aggregates, dict)) or not boot_aggregates:
                logger.error(
                    "Parse of measured boot event log has unexpected value for .boot_aggragtes: %r", boot_aggregates
                )
                failure.add_event("invalid_boot_aggregates", {"got": boot_aggregates}, True)
                return {}, None, {}, failure

            return pcr_hashes, boot_aggregates, mb_measurement_data, failure

        return {}, None, {}, failure
