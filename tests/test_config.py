import pytest

from attention_budget.config import load_config


def test_defaults_match_spec(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[deepseek]\napi_key="k"\nprice_in_per_m=1\nprice_out_per_m=2\n'
                 '[telegram]\nbot_token="t"\nchat_id=42\n', encoding="utf-8")
    c = load_config(p)
    assert c.budget.daily_picks == 5 and c.budget.serendipity == 1
    assert c.caps.tokens == 500_000 and c.caps.tool_calls == 150 and c.caps.per_item_tools == 4
    assert c.volumes.xhs_recommend == 50 and c.volumes.deep_pool == 30
    assert c.xhs.detail_cap == 40 and c.xhs.ocr_images == 6
    assert c.deepseek.model == "deepseek-chat"


def test_missing_chat_id_names_the_key(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[deepseek]\napi_key="k"\nprice_in_per_m=1\nprice_out_per_m=2\n[telegram]\nbot_token="t"\n',
                 encoding="utf-8")
    with pytest.raises(ValueError, match="telegram.chat_id"):
        load_config(p)


def test_more_than_ten_key_creators_rejected(tmp_path):
    p = tmp_path / "config.toml"
    links = ",".join(f'"https://www.xiaohongshu.com/user/profile/u{i}"' for i in range(11))
    p.write_text('[deepseek]\napi_key="k"\nprice_in_per_m=1\nprice_out_per_m=2\n'
                 f'[telegram]\nbot_token="t"\nchat_id=42\n[xhs]\nkey_creators=[{links}]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="key_creators"):
        load_config(p)
