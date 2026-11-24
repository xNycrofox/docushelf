"""settings.yaml written from the web interface: validation, backup, locks and secrets."""

from __future__ import annotations

import stat

import pytest
import yaml

from docushelf.config import load_settings
from docushelf.errors import ConfigError
from docushelf.settings_file import SettingsFile, apply_live, dump_settings


@pytest.fixture
def store(env, data_dir):
    return SettingsFile(data_dir, env=env)


def file_values(store):
    return yaml.safe_load(store.path.read_text(encoding="utf-8")) or {}


def test_save_writes_validated_values_and_keeps_a_backup(store, env):
    settings, changed = store.save({"ocr_jobs": 4, "ocr_languages": "deu+fra"})
    assert changed == ["ocr_jobs", "ocr_languages"]
    assert settings.ocr_jobs == 4 and settings.ocr_languages == "deu+fra"
    assert file_values(store) == {"ocr_jobs": 4, "ocr_languages": "deu+fra"}
    assert store.path.read_text(encoding="utf-8").startswith("# DocuShelf")
    assert not store.backup_path.exists()
    # The loader of the server reads it.
    assert load_settings(env).ocr_jobs == 4

    first = store.path.read_text(encoding="utf-8")
    _settings, changed = store.save({"ocr_jobs": 6, "ocr_languages": None})
    assert changed == ["ocr_jobs", "ocr_languages"]
    assert store.backup_path.read_text(encoding="utf-8") == first
    assert file_values(store) == {"ocr_jobs": 6}

    # Nothing changed: nothing is written.
    before = store.path.stat().st_mtime_ns
    assert store.save({"ocr_jobs": 6})[1] == []
    assert store.path.stat().st_mtime_ns == before


def test_invalid_values_are_refused_and_nothing_is_written(store):
    store.save({"ocr_jobs": 3})
    text = store.path.read_text(encoding="utf-8")
    with pytest.raises(ConfigError, match="LLM_URL"):
        store.save({"llm_enabled": True, "llm_provider": "openai"})  # an OpenAI-compatible server needs an address
    with pytest.raises(ConfigError):
        store.save({"auto_file": "manchmal"})
    assert store.path.read_text(encoding="utf-8") == text
    assert not store.backup_path.exists()


def test_a_check_may_refuse_the_result(store):
    def refuse(settings):
        raise ConfigError("nein")

    with pytest.raises(ConfigError, match="nein"):
        store.save({"ocr_jobs": 5}, check=refuse)
    assert not store.path.exists()


def test_environment_variables_lock_their_keys(env, data_dir):
    store = SettingsFile(data_dir, env={**env, "DOCUSHELF_OCR_JOBS": "8", "DOCUSHELF_LLM_API_KEY_FILE": "/run/secrets/x"})
    assert store.env_source("ocr_jobs") == "DOCUSHELF_OCR_JOBS"
    assert store.env_source("llm_api_key") == "DOCUSHELF_LLM_API_KEY_FILE"
    assert store.env_source("ocr_languages") is None
    assert store.env_value("ocr_jobs") == "8" and store.env_value("llm_api_key") is None
    with pytest.raises(ConfigError, match="DOCUSHELF_OCR_JOBS"):
        store.save({"ocr_jobs": 2})
    with pytest.raises(ConfigError, match="DOCUSHELF_LLM_API_KEY"):
        store.save({}, secrets={"llm_api_key": "neu"})
    # The environment keeps winning over the file.
    (data_dir / "config").mkdir(exist_ok=True)
    store.path.write_text("ocr_jobs: 2\n", encoding="utf-8")
    assert store.load().ocr_jobs == 8


def test_secrets_live_in_their_own_files(store, env, data_dir):
    settings, changed = store.save(
        {"llm_enabled": True, "llm_model": "qwen2.5:7b"}, secrets={"llm_api_key": "  sk-sehr-geheim  "}
    )
    assert "llm_api_key" in changed and settings.llm.api_key == "sk-sehr-geheim"
    secret = data_dir / "config" / "secrets" / "llm-api-key"
    assert secret.read_text(encoding="utf-8").strip() == "sk-sehr-geheim"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert stat.S_IMODE(secret.parent.stat().st_mode) == 0o700
    text = store.path.read_text(encoding="utf-8")
    assert "sk-sehr-geheim" not in text and "llm_api_key_file: secrets/llm-api-key" in text
    assert load_settings(env).llm.api_key == "sk-sehr-geheim"
    assert store.has_secret("llm_api_key")

    # An empty value keeps the secret, None removes it.
    assert store.save({}, secrets={"llm_api_key": ""})[1] == []
    _settings, changed = store.save({}, secrets={"llm_api_key": None})
    assert changed == ["llm_api_key"] and not secret.exists()
    assert "llm_api_key" not in store.path.read_text(encoding="utf-8")
    assert not store.has_secret("llm_api_key")


def test_a_secret_is_written_only_when_everything_is_valid(store, data_dir):
    with pytest.raises(ConfigError):
        store.save({"llm_enabled": True, "llm_provider": "openai"}, secrets={"llm_api_key": "sk-x"})
    assert not (data_dir / "config" / "secrets" / "llm-api-key").exists()


def test_secret_references_in_settings_yaml(env, data_dir):
    config = data_dir / "config"
    (config / "secrets").mkdir(parents=True)
    (config / "secrets" / "oidc").write_text("client-geheim\n", encoding="utf-8")
    (config / "settings.yaml").write_text("oidc_client_secret_file: secrets/oidc\n", encoding="utf-8")
    assert load_settings(env).oidc_client_secret == "client-geheim"
    (config / "settings.yaml").write_text("oidc_client_secret_file: secrets/fehlt\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="oidc_client_secret_file"):
        load_settings(env)
    # The environment wins over the file.
    assert load_settings({**env, "DOCUSHELF_OIDC_CLIENT_SECRET": "aus-env"}).oidc_client_secret == "aus-env"


def test_file_values_replace_the_file_for_checks(env, data_dir):
    (data_dir / "config").mkdir(exist_ok=True)
    (data_dir / "config" / "settings.yaml").write_text("ocr_jobs: 3\n", encoding="utf-8")
    assert load_settings(env).ocr_jobs == 3
    assert load_settings(env, file_values={"ocr_jobs": 7}).ocr_jobs == 7
    assert load_settings(env, file_values={}).ocr_jobs == 2


def test_dump_settings_is_sorted_and_commented():
    text = dump_settings({"b": 1, "a": [1, 2]})
    assert text.startswith("# DocuShelf") and text.index("a:") < text.index("b:")
    assert dump_settings({}).count("\n") == 3


def test_apply_live_updates_running_containers(services):
    services.settings = services.settings.with_overrides(notify_url=None)
    apply_live([services, None], {"notify_url": "https://ntfy.example.test/x", "trash_retention_days": 9})
    assert services.settings.notify_url == "https://ntfy.example.test/x"
    assert services.settings.trash_retention_days == 9
    assert services.channels.base_settings.trash_retention_days == 9
    assert services.notifier.url == "https://ntfy.example.test/x"
    # The channel manager keeps the change when it applies its own.
    services.channels.apply()
    assert services.settings.trash_retention_days == 9
