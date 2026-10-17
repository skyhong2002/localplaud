"""Interpreter-startup isolation, inherited by every synthetic Python child."""

import socket
import sys


def audit(event, args):
    if event == "socket.__new__" and args[1] in (socket.AF_INET, socket.AF_INET6):
        raise RuntimeError("RUNTIME_TEST_OUTBOUND_NETWORK_BLOCKED")
    if event in {
        "socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
        "socket.gethostbyaddr", "socket.sendto", "socket.sendmsg",
    }:
        if event != "socket.connect" or args[0].family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError("RUNTIME_TEST_OUTBOUND_NETWORK_BLOCKED")
    if event == "import" and args[0].split(".")[0] in {
        "torch", "transformers", "faster_whisper", "mlx", "mlx_whisper", "pyannote",
        "nemo", "silero_vad", "huggingface_hub", "sentence_transformers",
    }:
        raise RuntimeError("RUNTIME_TEST_REAL_MODEL_IMPORT_BLOCKED")


sys.addaudithook(audit)

# Install the audit hook before importing application code. Fail closed if this
# prerequisite cannot load: Python normally continues after sitecustomize errors.
try:
    import localplaud.config as config

    config.Settings.model_config["env_file"] = None
    config.load_dotenv = lambda *args, **kwargs: False
    sys._localplaud_runtime_guard = True
except BaseException:
    raise SystemExit("runtime test startup guard could not disable .env loading") from None
