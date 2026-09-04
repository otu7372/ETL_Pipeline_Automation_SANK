"""
config_loader.py
-----------------
Loads config/config.yaml once and hands it out to every other module.
Keeping this in one place means the whole suite (Stage 1 through Stage 6)
reads connection details the same way, so nothing drifts between scripts.
"""

import os
import yaml

# Project root = one level above /src
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.yaml")


def load_config(path: str = CONFIG_PATH) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Config file not found at {path}. "
            f"Copy config/config.yaml and fill in your credentials."
        )
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)

    # Fail fast and loudly if the password placeholder was never filled in,
    # rather than letting mysql-connector give a cryptic auth error later.
    pwd = cfg.get("mysql", {}).get("password", "")
    if not pwd or pwd == "<FILL IN>":
        raise ValueError(
            "mysql.password is not set in config.yaml. "
            "Open config/config.yaml and fill in MYSQL_LEARNER_PASSWORD from your .env."
        )
    return cfg
