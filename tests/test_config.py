import json
import stat

from ominime.config import AppConfig


def test_saved_config_has_no_multimodal_qwen_fields(tmp_path):
    config = AppConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "ominime.db",
        log_dir=tmp_path / "logs",
    )
    path = tmp_path / "config.json"

    config.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "multimodal_context_analysis" not in payload
    assert "multimodal_backend" not in payload
    assert not any(key.startswith("qwen_vl_") for key in payload)
    assert not any(key.startswith("openai_") for key in payload)


def test_openai_environment_is_not_part_of_application_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_MODEL", "remote-model")

    config = AppConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "ominime.db",
        log_dir=tmp_path / "logs",
    )

    assert not hasattr(config, "openai_api_key")
    assert not hasattr(config, "openai_model")
    assert config.ai_enabled is False


def test_loading_existing_config_tightens_file_permissions(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"ai_enabled": false}', encoding="utf-8")
    path.chmod(0o644)

    AppConfig.load(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_explicit_ai_environment_overrides_saved_local_preference(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"ai_enabled": false}', encoding="utf-8")
    monkeypatch.setenv("AI_ENABLED", "true")

    config = AppConfig.load(path)

    assert config.ai_enabled is True


def test_post_send_chat_all_apps_roundtrip_and_rollback(tmp_path):
    config = AppConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "ominime.db",
        log_dir=tmp_path / "logs",
    )
    assert config.post_send_chat_all_apps is True

    path = tmp_path / "config.json"
    config.post_send_chat_all_apps = False
    config.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["post_send_chat_all_apps"] is False
    restored = AppConfig.load(path)
    assert restored.post_send_chat_all_apps is False

    # A pre-existing config without the key keeps the default-on semantics.
    path.write_text('{"ai_enabled": false}', encoding="utf-8")
    assert AppConfig.load(path).post_send_chat_all_apps is True
