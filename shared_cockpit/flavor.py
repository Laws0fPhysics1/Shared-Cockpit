"""Which simulator this run talks to. Chosen at startup with select()."""
import os

FLAVORS = {
    "2024": {
        "SIM": "MSFS 2024",
        "SDK_INDEX": "https://sdk.flightsimulator.com/msfs2024/files/sdk.json",
        "SDK_ENV": "MSFS2024_SDK",
        "SDK_DEFAULT_DIRS": [r"C:\MSFS 2024 SDK"],
    },
    "2020": {
        "SIM": "MSFS 2020",
        "SDK_INDEX": "https://sdk.flightsimulator.com/files/sdk.json",
        "SDK_ENV": "MSFS_SDK",
        "SDK_DEFAULT_DIRS": [r"C:\MSFS SDK"],
    },
    "dry": {
        "SIM": "Dry run (no sim)",
        "SDK_INDEX": "",
        "SDK_ENV": "",
        "SDK_DEFAULT_DIRS": [],
    },
}
LABELS = {k: v["SIM"] for k, v in FLAVORS.items()}

KEY = "2024"
SIM = SDK_INDEX = SDK_ENV = ""
SDK_DEFAULT_DIRS = []


def select(key):
    global KEY, SIM, SDK_INDEX, SDK_ENV, SDK_DEFAULT_DIRS
    if key not in FLAVORS:
        key = "2024"
    KEY = key
    f = FLAVORS[key]
    SIM, SDK_INDEX, SDK_ENV, SDK_DEFAULT_DIRS = f["SIM"], f["SDK_INDEX"], f["SDK_ENV"], f["SDK_DEFAULT_DIRS"]


def is_dry():
    return KEY == "dry"


def detect_installed():
    """Best-effort list of installed sims ("2024", "2020"), newest first. No subprocesses."""
    la = os.environ.get("LOCALAPPDATA", "")
    ad = os.environ.get("APPDATA", "")
    found = []
    if os.path.isdir(os.path.join(la, "Packages", "Microsoft.Limitless_8wekyb3d8bbwe")) or \
            os.path.isdir(os.path.join(ad, "Microsoft Flight Simulator 2024")):
        found.append("2024")
    if os.path.isdir(os.path.join(la, "Packages", "Microsoft.FlightSimulator_8wekyb3d8bbwe")) or \
            os.path.isdir(os.path.join(ad, "Microsoft Flight Simulator")):
        found.append("2020")
    return found


select("2024")
