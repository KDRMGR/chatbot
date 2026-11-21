import pytest

from app.config import RateLimitConfig, Settings, load_settings


def test_defaults_match_the_phase1_findings(tmp_path):
    s = Settings(state_dir=tmp_path)
    assert s.primary_model == "qwen3:4b-instruct-2507-q4_K_M" and s.fallback_model == "qwen3:1.7b"
    assert (s.num_ctx, s.num_gpu, s.keep_alive) == (4096, 99, "30m")
    assert s.data_dir == tmp_path / "data" and s.log_dir == tmp_path / "logs"
    assert s.database_path == tmp_path / "data" / "stealthbot.db"
    assert s.limits_for("youtube").max_chars == 200 and s.limits_for("simulator").user_cooldown_s == 0


def test_environment_overrides_are_typed(tmp_path):
    env = {
        "STEALTHBOT_STATE_DIR": str(tmp_path), "STEALTHBOT_PORT": "9001", "STEALTHBOT_NUM_GPU": "none",
        "STEALTHBOT_AI_ENABLED": "false", "STEALTHBOT_TEMPERATURE": "0.5", "STEALTHBOT_FALLBACK_MODEL": "",
        "STEALTHBOT_ALLOWED_COMMANDS": "!a, !b", "STEALTHBOT_CURRENT_GAME": "Some Game",
        "STEALTHBOT_RATE_LIMITS": '{"youtube": {"min_gap_s": 20}}',
    }
    s = load_settings(env, env_file=None)
    assert (s.port, s.num_gpu, s.ai_enabled, s.temperature) == (9001, None, False, 0.5)
    assert s.fallback_model is None and s.allowed_commands == ["!a", "!b"] and s.current_game == "Some Game"
    assert s.limits_for("youtube").min_gap_s == 20 and s.limits_for("youtube").max_chars == 200  # merged with defaults
    assert s.limits_for("kick").max_chars == 500  # untouched platforms keep their defaults


def test_env_file_is_read_and_real_environment_wins(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nSTEALTHBOT_STREAMER_NAME='FromFile'\nSTEALTHBOT_CURRENT_GAME=File Game\n", encoding="utf-8")
    s = load_settings({"STEALTHBOT_CURRENT_GAME": "Real Env", "STEALTHBOT_STATE_DIR": str(tmp_path)}, env_file=env_file)
    assert s.streamer_name == "FromFile" and s.current_game == "Real Env"


def test_env_file_inline_comments_are_not_part_of_the_value(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "STEALTHBOT_PORT=9100          # the control API\n"
        "STEALTHBOT_KICK_WEBHOOK_PUBLIC_URL=https://x.example/kick/webhook   # shown in the dashboard only\n"
        "STEALTHBOT_CURRENT_GAME=\"Game # 2\"  # a hash inside quotes stays\n"
        "STEALTHBOT_STREAMER_NAME=Name#NoSpace\n", encoding="utf-8")
    s = load_settings({"STEALTHBOT_STATE_DIR": str(tmp_path)}, env_file=env_file)
    assert s.port == 9100
    assert s.kick_webhook_public_url == "https://x.example/kick/webhook"
    assert s.current_game == "Game # 2"
    assert s.streamer_name == "Name#NoSpace"  # no whitespace before '#': not a comment


def test_invalid_values_fail_loudly(tmp_path):
    with pytest.raises(ValueError, match="STEALTHBOT_PORT"):
        load_settings({"STEALTHBOT_PORT": "abc"}, env_file=None)
    with pytest.raises(ValueError):
        Settings(state_dir=tmp_path, port=70000)
    with pytest.raises(ValueError):
        Settings(state_dir=tmp_path, temperature=5)


def test_youtube_defaults_are_safe(tmp_path):
    s = Settings(state_dir=tmp_path)
    assert s.youtube_enabled is False and s.youtube_use_streaming is False and s.youtube_skip_backlog is True
    assert s.youtube_daily_quota == 10_000 and s.youtube_data_retention_days == 30
    # 7 messages / 10 min * 20 units keeps a 6 h stream inside the default quota together with chat reading
    assert s.limits_for("youtube").max_per_window == 7 and s.limits_for("youtube").max_chars == 200


def test_youtube_retention_cannot_exceed_googles_30_day_limit(tmp_path):
    for bad in (0, 31, 365):
        with pytest.raises(ValueError, match="30"):
            Settings(state_dir=tmp_path, youtube_data_retention_days=bad)
    assert Settings(state_dir=tmp_path, youtube_data_retention_days=7).youtube_data_retention_days == 7
    assert Settings(state_dir=tmp_path).youtube_send_budget_fraction == 0.4
    for bad in (-0.1, 0.9):
        with pytest.raises(ValueError, match="send_budget"):
            Settings(state_dir=tmp_path, youtube_send_budget_fraction=bad)


def test_youtube_environment_overrides(tmp_path):
    env = {"STEALTHBOT_STATE_DIR": str(tmp_path), "STEALTHBOT_YOUTUBE_ENABLED": "true",
           "STEALTHBOT_YOUTUBE_VIDEO_ID": "abc123", "STEALTHBOT_YOUTUBE_USE_STREAMING": "yes"}
    s = load_settings(env, env_file=None)
    assert s.youtube_enabled and s.youtube_video_id == "abc123" and s.youtube_use_streaming
    with pytest.raises(ValueError):
        Settings(state_dir=tmp_path, youtube_poll_active_s=0)


def test_dict_rate_limits_are_converted(tmp_path):
    s = Settings(state_dir=tmp_path, rate_limits={"kick": {"min_gap_s": 3, "max_per_window": 5}})
    assert isinstance(s.rate_limits["kick"], RateLimitConfig) and s.rate_limits["kick"].min_gap_s == 3
