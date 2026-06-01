"""集中从 .env 读配置。字段名小写,自动匹配大写 env 变量。"""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    athenai_api_key: str
    athenai_base_url: str = "https://athenai.mihoyo.com"

    helper_data_dir: Path = Path("./var/helper")
    helper_spec_git_dir: Path = Path("./var/helper/git-repo")

    wave_app_id: str = ""
    wave_app_secret: str = ""
    wave_callback_aes_key: str = ""
    wave_callback_sign_token: str = ""
    # Wave 开放平台 HTTP API(服务端 app_id+app_secret 自换 access_token)。
    # NOT openapi-mcp — MCP 只能用登录用户身份,不能服务端用。
    wave_open_api_base_url: str = "https://open.hoyowave.com"
    # KM 开放平台 HTTP API(文档检索 / 文档读取 / 表格读取)。
    # 实际入口跟 wave 同一个 host(open.hoyowave.com) — 官方文档 mhayl60navc8/mhisg59mwgzu 确认。
    # 凭据复用 wave_app_id + wave_app_secret,但 token 独立缓存(token 域不假设互通)。
    km_open_api_base_url: str = "https://open.hoyowave.com"
    # 默认米哈游租户(union_id ↔ 域账号互转时需要)。海外站换 cognosphere 那个。
    wave_user_tenant_id: str = "ot_9c253a6cbabafcaf131ca0ab549049db"

    helper_admin_sk: str = ""

    # Inbox 周报 push 对象 — M1/M2 dogfood 阶段单 owner 设计:
    # 周报每周一 9:00 CST push 给这一人,内容是全局所有 pending 项(冲突/追问/spec候选)。
    # M3 多专家时再拆 per-author。空 → 不自动创建默认 inbox_weekly 任务(管理员可手动建)。
    helper_owner_domain: str = ""

    log_level: str = "INFO"

    # L1 抽取 prompt 版本。v1=5 类细分(decision/fact/case/concept/relation),
    # v2=二分(section + decision)。dogfood 期默认 v1,v2 通过 dryrun CLI 验证后切。
    l1_prompt_version: str = "v1"

    @field_validator("helper_data_dir", "helper_spec_git_dir", mode="after")
    @classmethod
    def _resolve_path(cls, v: Path) -> Path:
        return v.expanduser().resolve()

    @property
    def wave_callback_configured(self) -> bool:
        return bool(
            self.wave_app_id
            and self.wave_callback_aes_key
            and self.wave_callback_sign_token
        )

    @property
    def admin_enabled(self) -> bool:
        # 空 sk → admin 路由整体 404,不暴露探测面
        return bool(self.helper_admin_sk)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
