import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    access_key: str = os.getenv("OSS_ACCESS_KEY", "")
    secret_key: str = os.getenv("OSS_SECRET_KEY", "")
    endpoint: str = os.getenv("OSS_ENDPOINT", "https://oss.fandow.com").rstrip("/")
    bucket: str = os.getenv("OSS_BUCKET", "marketing-video-dashboard")
    prefix: str = os.getenv("OSS_PREFIX", "yxb/").lstrip("/")
    public_base_url: str = os.getenv("OSS_PUBLIC_BASE_URL", "").rstrip("/")
    workstation_api_token: str = os.getenv("WIS_WORKSTATION_API_TOKEN", "").strip()
    remix_worker_base_url: str = os.getenv("WIS_REMIX_WORKER_BASE_URL", "").strip().rstrip("/")
    remix_worker_token: str = (
        os.getenv("WIS_REMIX_WORKER_TOKEN")
        or os.getenv("WIS_WORKSTATION_API_TOKEN")
        or ""
    ).strip()
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./wis_video_center.db")
    trash_retention_days: int = max(1, int(os.getenv("TRASH_RETENTION_DAYS", "7")))
    trash_cleanup_interval_days: int = max(1, int(os.getenv("TRASH_CLEANUP_INTERVAL_DAYS", "7")))
    catalog_cache_seconds: int = max(60, int(os.getenv("CATALOG_CACHE_SECONDS", "600")))
    upload_global_part_limit: int = min(64, max(2, int(os.getenv("UPLOAD_GLOBAL_PART_LIMIT", "24"))))
    upload_user_part_limit: int = min(16, max(1, int(os.getenv("UPLOAD_USER_PART_LIMIT", "8"))))
    upload_lease_seconds: int = min(300, max(30, int(os.getenv("UPLOAD_LEASE_SECONDS", "120"))))
    upload_session_hours: int = min(72, max(2, int(os.getenv("UPLOAD_SESSION_HOURS", "24"))))
    qianchuan_metrics_interval_minutes: int = max(60, int(os.getenv("QIANCHUAN_METRICS_INTERVAL_MINUTES", "120")))
    qianchuan_metrics_record_min_age_minutes: int = max(60, int(os.getenv("QIANCHUAN_METRICS_RECORD_MIN_AGE_MINUTES", "120")))
    qianchuan_metrics_auto_batch_size: int = min(50, max(1, int(os.getenv("QIANCHUAN_METRICS_AUTO_BATCH_SIZE", "30"))))
    qianchuan_metrics_manual_batch_size: int = min(50, max(1, int(os.getenv("QIANCHUAN_METRICS_MANUAL_BATCH_SIZE", "50"))))
    qianchuan_metrics_manual_cooldown_minutes: int = max(5, int(os.getenv("QIANCHUAN_METRICS_MANUAL_COOLDOWN_MINUTES", "10")))
    qianchuan_metrics_startup_delay_seconds: int = max(60, int(os.getenv("QIANCHUAN_METRICS_STARTUP_DELAY_SECONDS", "300")))
    qianchuan_metrics_daily_hour: int = min(23, max(0, int(os.getenv("QIANCHUAN_METRICS_DAILY_HOUR", "6"))))
    qianchuan_metrics_daily_lookback_days: int = min(30, max(1, int(os.getenv("QIANCHUAN_METRICS_DAILY_LOOKBACK_DAYS", "7"))))
    qianchuan_metrics_request_interval_seconds: float = max(0.2, float(os.getenv("QIANCHUAN_METRICS_REQUEST_INTERVAL_SECONDS", "1.0")))
    qianchuan_metrics_retry_count: int = min(5, max(1, int(os.getenv("QIANCHUAN_METRICS_RETRY_COUNT", "3"))))
    qianchuan_worker_limit: int = min(8, max(2, int(os.getenv("QIANCHUAN_WORKER_LIMIT", "4"))))
    cover_worker_limit: int = min(3, max(1, int(os.getenv("COVER_WORKER_LIMIT", "1"))))
    channels_metrics_daily_hour: int = min(23, max(0, int(os.getenv("CHANNELS_METRICS_DAILY_HOUR", "7"))))
    channels_metrics_daily_lookback_days: int = min(30, max(1, int(os.getenv("CHANNELS_METRICS_DAILY_LOOKBACK_DAYS", "7"))))
    channels_confirmation_interval_minutes: int = max(5, int(os.getenv("CHANNELS_CONFIRMATION_INTERVAL_MINUTES", "10")))
    channels_worker_limit: int = min(4, max(1, int(os.getenv("CHANNELS_WORKER_LIMIT", "2"))))
    channels_publish_transport: str = os.getenv("CHANNELS_PUBLISH_TRANSPORT", "auto").strip().lower()
    channels_internal_api_browser_fallback: bool = os.getenv("CHANNELS_INTERNAL_API_BROWSER_FALLBACK", "true").lower() in {"1", "true", "yes"}
    channels_direct_account_ids: str = os.getenv("CHANNELS_DIRECT_ACCOUNT_IDS", "").strip()
    channels_upload_chunk_bytes: int = min(
        32 * 1024 * 1024,
        max(1024 * 1024, int(os.getenv("CHANNELS_UPLOAD_CHUNK_BYTES", str(8 * 1024 * 1024)))),
    )
    channels_upload_part_retries: int = min(5, max(1, int(os.getenv("CHANNELS_UPLOAD_PART_RETRIES", "3"))))
    channels_internal_request_timeout_seconds: int = min(
        300,
        max(30, int(os.getenv("CHANNELS_INTERNAL_REQUEST_TIMEOUT_SECONDS", "120"))),
    )
    channels_clip_timeout_seconds: int = min(
        3600,
        max(300, int(os.getenv("CHANNELS_CLIP_TIMEOUT_SECONDS", "1800"))),
    )
    adq_metrics_interval_minutes: int = max(60, int(os.getenv("ADQ_METRICS_INTERVAL_MINUTES", "120")))
    adq_metrics_record_min_age_minutes: int = max(60, int(os.getenv("ADQ_METRICS_RECORD_MIN_AGE_MINUTES", "120")))
    adq_metrics_auto_batch_size: int = min(50, max(1, int(os.getenv("ADQ_METRICS_AUTO_BATCH_SIZE", "30"))))
    adq_metrics_manual_batch_size: int = min(50, max(1, int(os.getenv("ADQ_METRICS_MANUAL_BATCH_SIZE", "50"))))
    adq_metrics_manual_cooldown_minutes: int = max(5, int(os.getenv("ADQ_METRICS_MANUAL_COOLDOWN_MINUTES", "10")))
    adq_metrics_startup_delay_seconds: int = max(60, int(os.getenv("ADQ_METRICS_STARTUP_DELAY_SECONDS", "300")))
    adq_shared_source_account_id: str = os.getenv("ADQ_SHARED_SOURCE_ACCOUNT_ID", "80431518").strip()
    adq_shared_mdm_id: str = os.getenv("ADQ_SHARED_MDM_ID", "33471608").strip()
    fandow_data_mcp_endpoint: str = os.getenv(
        "FANDOW_DATA_MCP_ENDPOINT",
        "http://cloud.fandow.com/gpt/ai-platform/mcp/data/",
    ).strip()
    fandow_data_mcp_token: str = (
        os.getenv("FANDOW_DATA_MCP_TOKEN")
        or os.getenv("FANDOM_DATA_MCP_TOKEN")
        or ""
    ).strip()
    fandow_data_mcp_timeout_seconds: int = max(
        30,
        int(os.getenv("FANDOW_DATA_MCP_TIMEOUT_SECONDS", "120")),
    )
    jump_llm_url: str = os.getenv(
        "JUMP_LLM_URL",
        "https://cloud.fandow.com/gpt/interface/chat/completions",
    ).strip()
    jump_llm_token: str = (
        os.getenv("JUMP_LLM_TOKEN")
        or os.getenv("FANDOW_DATA_MCP_TOKEN")
        or os.getenv("FANDOM_DATA_MCP_TOKEN")
        or ""
    ).strip()
    jump_llm_application: str = os.getenv("JUMP_LLM_APPLICATION", "pingying_zhongshu").strip()
    jump_llm_provider: str = os.getenv("JUMP_LLM_PROVIDER", "deepseek").strip()
    jump_llm_model: str = os.getenv("JUMP_LLM_MODEL", "deepseek-v4-pro").strip()
    jump_llm_vision_model: str = os.getenv(
        "JUMP_LLM_VISION_MODEL",
        "deepseek-v4-flash-vision-exp",
    ).strip()
    jump_llm_timeout_seconds: int = min(180, max(30, int(os.getenv("JUMP_LLM_TIMEOUT_SECONDS", "120"))))
    jump_llm_temperature: float = min(1.0, max(0.0, float(os.getenv("JUMP_LLM_TEMPERATURE", "0.45"))))
    ai_assistant_upload_dir: str = os.getenv(
        "AI_ASSISTANT_UPLOAD_DIR",
        "/data/assistant_uploads",
    ).strip()
    ai_assistant_image_max_bytes: int = min(
        12 * 1024 * 1024,
        max(1024 * 1024, int(os.getenv("AI_ASSISTANT_IMAGE_MAX_BYTES", str(8 * 1024 * 1024)))),
    )
    ai_insight_interval_seconds: int = min(
        3600,
        max(300, int(os.getenv("AI_INSIGHT_INTERVAL_SECONDS", "900"))),
    )
    personal_sales_snapshot_path: str = os.getenv(
        "PERSONAL_SALES_SNAPSHOT_PATH",
        "/data/personal_sales_snapshot.json",
    ).strip()
    personal_sales_refresh_hour: int = min(
        23,
        max(0, int(os.getenv("PERSONAL_SALES_REFRESH_HOUR", "10"))),
    )
    personal_sales_startup_delay_seconds: int = max(
        30,
        int(os.getenv("PERSONAL_SALES_STARTUP_DELAY_SECONDS", "90")),
    )
    personal_sales_retry_interval_minutes: int = max(
        10,
        int(os.getenv("PERSONAL_SALES_RETRY_INTERVAL_MINUTES", "30")),
    )
    business_intelligence_snapshot_path: str = os.getenv(
        "BUSINESS_INTELLIGENCE_SNAPSHOT_PATH",
        "/data/business_intelligence_snapshot.json",
    ).strip()
    business_intelligence_startup_delay_seconds: int = max(
        10,
        int(os.getenv("BUSINESS_INTELLIGENCE_STARTUP_DELAY_SECONDS", "20")),
    )
    business_intelligence_retry_interval_minutes: int = max(
        10,
        int(os.getenv("BUSINESS_INTELLIGENCE_RETRY_INTERVAL_MINUTES", "30")),
    )
    long_term_work_snapshot_path: str = os.getenv(
        "LONG_TERM_WORK_SNAPSHOT_PATH",
        "/data/long_term_work_scheduler.json",
    ).strip()
    long_term_work_chat_id: str = os.getenv(
        "LONG_TERM_WORK_CHAT_ID",
        "oc_04ec28cf2911911da8c202c5eee21742",
    ).strip()
    long_term_work_chat_name: str = os.getenv(
        "LONG_TERM_WORK_CHAT_NAME",
        "品牌营销部-核心干将",
    ).strip()
    long_term_work_enabled: bool = os.getenv(
        "LONG_TERM_WORK_ENABLED",
        "true",
    ).strip().lower() in {"1", "true", "yes", "on"}
    long_term_work_daily_hour: int = min(
        23,
        max(0, int(os.getenv("LONG_TERM_WORK_DAILY_HOUR", "10"))),
    )
    long_term_work_daily_minute: int = min(
        59,
        max(0, int(os.getenv("LONG_TERM_WORK_DAILY_MINUTE", "15"))),
    )
    long_term_work_lookback_days: int = min(
        30,
        max(1, int(os.getenv("LONG_TERM_WORK_LOOKBACK_DAYS", "7"))),
    )
    asset_admin_numbers: tuple[str, ...] = tuple(
        value.strip()
        for value in os.getenv("ASSET_ADMIN_NUMBERS", "").split(",")
        if value.strip()
    )
    cors_origins: tuple[str, ...] = tuple(
        value.strip()
        for value in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
        if value.strip()
    )

    @property
    def oss_configured(self) -> bool:
        return bool(self.access_key and self.secret_key and self.endpoint and self.bucket)


settings = Settings()
