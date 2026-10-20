"""Configuration loaded from config.toml in the data directory."""
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

MAX_KEY_CREATORS = 10


class DeepSeekCfg(BaseModel):
    api_key: str
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com"
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0


class TelegramCfg(BaseModel):
    bot_token: str
    chat_id: int


class ScheduleCfg(BaseModel):
    push_time: str = "08:00"
    review_weekday: int = 6  # Monday=0 … Sunday=6
    review_time: str = "20:00"


class Budget(BaseModel):
    daily_picks: int = 5
    serendipity: int = 1


class Caps(BaseModel):
    tokens: int = 500_000
    tool_calls: int = 150
    minutes: int = 40
    per_item_tools: int = 4


class Volumes(BaseModel):
    bili_follow: int = 100
    bili_recommend: int = 50
    xhs_recommend: int = 50
    xhs_creator_notes: int = 10
    searches_per_platform: int = 3
    results_per_search: int = 20
    coarse_batch: int = 50
    deep_pool: int = 30
    rescue: int = 5


class XhsCfg(BaseModel):
    exe_path: Path | None = None
    port: int = 18060
    delay_min: float = 3.0
    delay_max: float = 8.0
    detail_cap: int = 40
    ocr_images: int = 6
    key_creators: list[str] = []

    @field_validator("key_creators")
    @classmethod
    def _limit(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_KEY_CREATORS:
            raise ValueError(f"xhs.key_creators 最多 {MAX_KEY_CREATORS} 个，现在有 {len(v)} 个")
        return v


class Config(BaseModel):
    deepseek: DeepSeekCfg
    telegram: TelegramCfg
    schedule: ScheduleCfg = Field(default_factory=ScheduleCfg)
    budget: Budget = Field(default_factory=Budget)
    caps: Caps = Field(default_factory=Caps)
    volumes: Volumes = Field(default_factory=Volumes)
    xhs: XhsCfg = Field(default_factory=XhsCfg)
    note_default_days: int = 14
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".attention-budget")


def load_config(path: Path) -> Config:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        problems = []
        for err in e.errors():
            key = ".".join(str(p) for p in err["loc"])
            problems.append(f"{key}：{'缺少这一项' if err['type'] == 'missing' else err['msg']}")
        raise ValueError("config.toml 有问题 —— " + "；".join(problems)) from None
