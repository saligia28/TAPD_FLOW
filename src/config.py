from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import List, Optional

# Best-effort load .env if python-dotenv is available
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass


@dataclass
class Config:
    # OpenAPI (AppKey/Secret) - optional, not needed for Basic Auth
    tapd_api_key: Optional[str] = None
    tapd_api_secret: Optional[str] = None
    tapd_workspace_id: Optional[str] = None

    # Basic Auth (per-user) for https://api.tapd.cn
    tapd_api_user: Optional[str] = None
    tapd_api_password: Optional[str] = None

    # Optional token (if TAPD later supports token-based auth)
    tapd_token: Optional[str] = None

    notion_token: Optional[str] = None
    notion_database_id: Optional[str] = None

    tz: str = os.getenv("TZ", "Asia/Shanghai")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # Endpoint config
    tapd_api_base: str = os.getenv("TAPD_API_BASE", "https://api.tapd.cn")
    tapd_stories_path: str = os.getenv("TAPD_STORIES_PATH", "/stories")
    tapd_modules_path: str = os.getenv("TAPD_MODULES_PATH", "/modules")
    tapd_iterations_path: str = os.getenv("TAPD_ITERATIONS_PATH", "/iterations")
    # Some tenants use different filter keys for stories-by-module; allow override
    tapd_module_filter_key: Optional[str] = os.getenv("TAPD_MODULE_FILTER_KEY")

    # Optional filters
    tapd_only_owner: Optional[str] = os.getenv("TAPD_ONLY_OWNER")
    tapd_only_creator: Optional[str] = os.getenv("TAPD_ONLY_CREATOR")
    tapd_use_current_iteration: bool = os.getenv("TAPD_USE_CURRENT_ITERATION", "").strip().lower() in {"1", "true", "yes", "on"}

    # Notion module value strategy: 'name' | 'path' (write full path like A/B/C)
    notion_module_value: str = os.getenv("NOTION_MODULE_VALUE", "name")

    # Advanced: allow multiple candidate filter keys for module filter compatibility
    tapd_filter_module_id_keys: List[str] = field(default_factory=list)
    tapd_filter_module_name_keys: List[str] = field(default_factory=list)
    tapd_filter_iteration_id_keys: List[str] = field(default_factory=list)
    module_path_sep: str = os.getenv("MODULE_PATH_SEP", "/")

    # Creation guards: restrict creating new Notion pages to owned-by substring and current iteration
    creation_owner_substr: Optional[str] = os.getenv("CREATION_OWNER_SUBSTR")
    creation_require_current_iteration: bool = os.getenv("CREATION_REQUIRE_CURRENT_ITERATION", "1").strip().lower() in {"1", "true", "yes", "on"}


REQUIRED_KEYS = [
    # For Notion writes; TAPD auth can be either Basic or AppKey/Secret
    "NOTION_TOKEN",
    "NOTION_DATABASE_ID",
]


def load_config() -> Config:
    def _csv(key: str, default: str) -> list[str]:
        raw = os.getenv(key, default)
        return [s.strip() for s in raw.split(',') if s.strip()]

    cfg = Config(
        # AppKey/Secret (OpenAPI)
        tapd_api_key=os.getenv("TAPD_API_KEY"),
        tapd_api_secret=os.getenv("TAPD_API_SECRET"),
        tapd_workspace_id=os.getenv("TAPD_WORKSPACE_ID"),

        # Per-user Basic credentials
        tapd_api_user=os.getenv("TAPD_API_USER"),
        tapd_api_password=os.getenv("TAPD_API_PASSWORD"),

        # Optional token
        tapd_token=os.getenv("TAPD_TOKEN"),

        # Notion
        notion_token=os.getenv("NOTION_TOKEN"),
        notion_database_id=os.getenv("NOTION_DATABASE_ID"),
        # Multi-key compatibility for module filter
        tapd_filter_module_id_keys=_csv("TAPD_FILTER_MODULE_ID_KEYS", "module_id,category_id,moduleid"),
        tapd_filter_module_name_keys=_csv("TAPD_FILTER_MODULE_NAME_KEYS", "module,category,module_name"),
        tapd_filter_iteration_id_keys=_csv("TAPD_FILTER_ITERATION_ID_KEYS", "iteration_id,sprint_id,iterationid"),
    )
    return cfg


def validate_config(cfg: Config) -> list[str]:
    missing: list[str] = []
    env = os.environ
    for k in REQUIRED_KEYS:
        if not env.get(k):
            missing.append(k)
    return missing
