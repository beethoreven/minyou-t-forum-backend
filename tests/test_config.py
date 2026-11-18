"""設定的讀取與檢查。這些測試不需要資料庫。"""

import json

import pytest

from config import DEFAULT_PORT, PROJECT_DIR, ConfigError, load_config

ENV = {
    "DATABASE_URL": "postgresql://localhost/example",
    "ALLOWED_ORIGINS": "http://localhost:8180",
    "GOOGLE_CLIENT_ID": "example.apps.googleusercontent.com",
    "FIRST_ADMIN_EMAIL": "Admin@Example.com",
    "DOCUMENTS_DIR": "local_documents",
}
SERVICE_ACCOUNT = json.dumps({"client_email": "bot@example.iam.gserviceaccount.com", "private_key": "secret-key"})
DRIVE_ENV = {key: value for key, value in ENV.items() if key != "DOCUMENTS_DIR"} | {
    "SERVICE_ACCOUNT_JSON": SERVICE_ACCOUNT,
    "DRIVE_FOLDER_ID": "1AbCdEfGhIjKlMnOpQrStUvWxYz",
}


def test_reads_every_setting():
    config = load_config(ENV | {"PORT": "6000", "SESSION_IDLE_DAYS": "30"})
    assert config.database_url == "postgresql://localhost/example"
    assert config.allowed_origins == ("http://localhost:8180",)
    assert config.google_client_id == "example.apps.googleusercontent.com"
    assert config.first_admin_email == "admin@example.com"  # 一律轉成小寫
    assert config.documents_dir == PROJECT_DIR / "local_documents"
    assert config.service_account_info is None
    assert config.port == 6000
    assert config.session_idle_days == 30


def test_optional_settings_have_defaults():
    config = load_config(ENV | {"PORT": " ", "SESSION_IDLE_DAYS": ""})
    assert config.port == DEFAULT_PORT
    assert config.session_idle_days == 90


def test_version_is_the_short_commit_the_host_reports():
    assert load_config(ENV).version == "local"

    deployed = load_config(ENV | {"RENDER_GIT_COMMIT": "C276936B2168BDE20B6447B303213808B1D766DC"})
    assert deployed.version == "c276936"

    for not_a_commit in ("", "main", "c27693", "not a commit; echo secret", "g" * 40):
        assert load_config(ENV | {"RENDER_GIT_COMMIT": not_a_commit}).version == "local"


def test_several_origins():
    config = load_config(ENV | {"ALLOWED_ORIGINS": "http://localhost:8180, https://example.github.io ,"})
    assert config.allowed_origins == ("http://localhost:8180", "https://example.github.io")


def test_absolute_documents_dir_is_kept(tmp_path):
    assert load_config(ENV | {"DOCUMENTS_DIR": str(tmp_path)}).documents_dir == tmp_path


def test_google_drive_as_the_documents_source():
    config = load_config(DRIVE_ENV)
    assert config.documents_dir is None
    assert config.drive_folder_id == "1AbCdEfGhIjKlMnOpQrStUvWxYz"
    assert config.service_account_info["client_email"] == "bot@example.iam.gserviceaccount.com"
    assert "secret-key" not in repr(config)  # 印出設定時不會帶出金鑰


@pytest.mark.parametrize(
    "changes, mentions",
    [
        ({"DOCUMENTS_DIR": "local_documents"}, "只能設定一種"),
        ({"SERVICE_ACCOUNT_JSON": ""}, "SERVICE_ACCOUNT_JSON"),
        ({"DRIVE_FOLDER_ID": ""}, "DRIVE_FOLDER_ID"),
        ({"SERVICE_ACCOUNT_JSON": "", "DRIVE_FOLDER_ID": ""}, "文件的來源"),
        ({"SERVICE_ACCOUNT_JSON": "not json"}, "SERVICE_ACCOUNT_JSON"),
        ({"SERVICE_ACCOUNT_JSON": '{"client_email": "bot@example.com"}'}, "SERVICE_ACCOUNT_JSON"),
        ({"DRIVE_FOLDER_ID": "https://drive.google.com/drive/folders/1AbCdEfGhIjKl"}, "DRIVE_FOLDER_ID"),
    ],
)
def test_rejects_a_broken_documents_source(changes, mentions):
    with pytest.raises(ConfigError, match=mentions) as error:
        load_config(DRIVE_ENV | changes)
    assert "secret-key" not in str(error.value)


def test_apps_script_is_optional_and_its_settings_come_as_a_pair():
    assert load_config(DRIVE_ENV).apps_script_url is None  # 沒設定就是不能從後台上傳，服務照樣起得來

    script = {"APPS_SCRIPT_URL": "https://script.google.com/macros/s/abc/exec", "APPS_SCRIPT_SECRET": "long-secret"}
    config = load_config(DRIVE_ENV | script)
    assert config.apps_script_url == script["APPS_SCRIPT_URL"]
    assert config.apps_script_secret == "long-secret"
    assert "long-secret" not in repr(config)


@pytest.mark.parametrize(
    "changes, mentions",
    [
        ({"APPS_SCRIPT_URL": "https://script.google.com/macros/s/abc/exec"}, "APPS_SCRIPT_SECRET"),
        ({"APPS_SCRIPT_SECRET": "long-secret"}, "APPS_SCRIPT_URL"),
        ({"APPS_SCRIPT_URL": "https://example.com/exec", "APPS_SCRIPT_SECRET": "long-secret"}, "APPS_SCRIPT_URL"),
    ],
)
def test_rejects_half_configured_apps_script(changes, mentions):
    with pytest.raises(ConfigError, match=mentions) as error:
        load_config(DRIVE_ENV | changes)
    assert "long-secret" not in str(error.value)


@pytest.mark.parametrize("key", ["DATABASE_URL", "ALLOWED_ORIGINS", "GOOGLE_CLIENT_ID", "FIRST_ADMIN_EMAIL"])
@pytest.mark.parametrize("blank", [None, "", "   "])
def test_missing_required_setting_names_the_key(key, blank):
    env = {name: value for name, value in ENV.items() if name != key}
    if blank is not None:
        env[key] = blank
    with pytest.raises(ConfigError, match=key):
        load_config(env)


@pytest.mark.parametrize("value", ['"postgresql://user:secret-password@host/db"', "'postgresql://host/db'"])
def test_rejects_a_value_wrapped_in_quotes(value):
    with pytest.raises(ConfigError, match="DATABASE_URL") as error:
        load_config(ENV | {"DATABASE_URL": value})
    assert "secret-password" not in str(error.value)


@pytest.mark.parametrize(
    "origins",
    [
        "http://localhost:8180/",
        "http://localhost:8180/forum",
        "localhost:8180",
        "ftp://localhost",
        "http://",
        ",",
        "http://localhost:8180, http://secret-host.example/",
    ],
)
def test_rejects_malformed_origins(origins):
    with pytest.raises(ConfigError, match="ALLOWED_ORIGINS") as error:
        load_config(ENV | {"ALLOWED_ORIGINS": origins})
    assert "secret-host" not in str(error.value)  # 訊息不印出設定的值


@pytest.mark.parametrize("email", ["not-an-email", "@example.com", "admin@", "a@b@c", "admin @example.com"])
def test_rejects_malformed_first_admin_email(email):
    with pytest.raises(ConfigError, match="FIRST_ADMIN_EMAIL"):
        load_config(ENV | {"FIRST_ADMIN_EMAIL": email})


@pytest.mark.parametrize("key", ["PORT", "SESSION_IDLE_DAYS"])
@pytest.mark.parametrize("value", ["abc", "0", "70000", "-1", "5180.0", "５１８０", "9" * 5000])
def test_rejects_invalid_numbers(key, value):
    with pytest.raises(ConfigError, match=key):
        load_config(ENV | {key: value})
