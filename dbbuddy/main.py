# ── Imports ────────────────────────────────────────────────────────────────
import getpass
import json
import logging
import os
import sys

from dbbuddy_core.models import DBConfig
from dbbuddy_core.pipeline import process_schema

# ── Logging Configuration ───────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    handlers=[logging.FileHandler("logs/dbbuddy.log"), logging.StreamHandler()],
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ── Output Writer ───────────────────────────────────────────────────────────
def write_output(semantic_layer, output_path: str = "output.json") -> str:
    """Atomically write semantic layer to a JSON file."""
    import os
    output_path = os.path.abspath(output_path)
    tmp_path = output_path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(semantic_layer, f, indent=2)
        os.replace(tmp_path, output_path)
    except Exception as e:
        logger.error(f"Output write failed: {str(e)}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return output_path

# ── Config Loader ───────────────────────────────────────────────────────────
def load_config() -> dict:
    """Load configuration from JSON file if --config flag is provided."""
    if "--config" in sys.argv:
        idx = sys.argv.index("--config")
        if idx + 1 < len(sys.argv):
            path = sys.argv[idx + 1]
            try:
                with open(path, "r") as f:
                    config = json.load(f)
                    logger.info(f"Configuration loaded from {path}")
                    return config
            except FileNotFoundError:
                logger.error(f"Config file not found: {path}")
                print(f"[-] Config file not found: {path}")
                sys.exit(1)
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON in config file: {str(e)}")
                print(f"[-] Invalid JSON in config file: {e}")
                sys.exit(1)
            except Exception as e:
                logger.error(f"Failed to load config: {str(e)}")
                print(f"[-] Failed to load config: {e}")
                sys.exit(1)
    return {}


# ── CLI Entry Point ──────────────────────────────────────────────────────────
def main():
    if "--version" in sys.argv:
        from dbbuddy import __version__
        print(f"dbbuddy v{__version__}")
        sys.exit(0)

    config = load_config()

    if "--ai" in sys.argv:
        config["ai"] = True

    if "--ai-provider" in sys.argv:
        idx = sys.argv.index("--ai-provider")
        if idx + 1 < len(sys.argv):
            config["ai_provider"] = sys.argv[idx + 1]

    host = config.get("host") or (input("Host [localhost]: ").strip() or "localhost")

    username = config.get("user") or ""
    while not username:
        username = input("Username: ").strip()
        if not username:
            print("[-] Username is required.")
    password = config.get("password") or getpass.getpass("Password: ")

    database = config.get("database") or ""
    while not database:
        database = input("Database: ").strip()
        if not database:
            print("[-] Database name is required.")
    pipeline_config = DBConfig(
        host=host,
        user=username,
        password=password,
        database=database,
        ai=config.get("ai", False),
        ai_provider=config.get("ai_provider", "local"),
        mapping_plugin=config.get("mapping_plugin", "default_mapping"),
    )

    result = process_schema(pipeline_config)

    if result is None:
        print("[-] Failed to process schema.")
        sys.exit(1)

    print(json.dumps(result, indent=2))
    return result

if __name__ == "__main__":
    main()
