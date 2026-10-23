obs = obslua
local function normalize_folder(value)
    local path = (value or ""):gsub("\\", "/")
    if path ~= "" and path:sub(-1) ~= "/" then path = path .. "/" end
    return path
end
local root = normalize_folder(script_path())
local write_error_logged = false
local output = nil
local last_revision = ""
local track = 2
local quality = 0
local last_status = 0
local start_deadline = 0
local was_active = false

local function report(message)
    local path = root .. "runtime/obs-status.txt"
    local f, err = io.open(path, "w")
    if not f then
        if not write_error_logged then
            obs.script_log(obs.LOG_ERROR, "OCS: 状態ファイルへ書き込めません: " .. path .. " / " .. tostring(err) .. "。OCS接続フォルダとSTART.cmdの起動を確認してください。")
            write_error_logged = true
        end
        return false
    end
    f:write(tostring(os.time()) .. "\n" .. message)
    f:close()
    write_error_logged = false
    return true
end

local function connection_test()
    obs.script_log(obs.LOG_INFO, "OCS 接続テスト / folder=" .. root)
    local state = output and obs.obs_output_active(output) and "active" or "idle"
    if report(state) then
        obs.script_log(obs.LOG_INFO, "OCS: 状態ファイルの書き込み成功")
    end
    return false
end

local function stop_output()
    if output then
        obs.obs_output_force_stop(output)
        obs.obs_output_release(output)
        output = nil
    end
end

local function start_output()
    stop_output()
    was_active = false
    start_deadline = os.time() + 3
    local settings = obs.obs_data_create()
    obs.obs_data_set_string(settings, "url", "udp://127.0.0.1:18791?pkt_size=1316")
    obs.obs_data_set_string(settings, "format_name", "mpegts")
    obs.obs_data_set_string(settings, "video_encoder", "libx264")
    obs.obs_data_set_string(settings, "audio_encoder", "aac")
    obs.obs_data_set_string(settings, "video_settings", "preset=ultrafast tune=zerolatency pix_fmt=yuv420p")
    obs.obs_data_set_string(settings, "muxer_settings", "mpegts_flags=resend_headers flush_packets=1")
    obs.obs_data_set_int(settings, "video_bitrate", quality == 1 and 3500 or 1800)
    obs.obs_data_set_int(settings, "audio_bitrate", 128)
    obs.obs_data_set_int(settings, "gop_size", 30)
    obs.obs_data_set_int(settings, "scale_width", quality == 1 and 1280 or 960)
    obs.obs_data_set_int(settings, "scale_height", quality == 1 and 720 or 540)
    output = obs.obs_output_create("ffmpeg_output", "caster-share-output", settings, nil)
    obs.obs_data_release(settings)
    if not output then report("error: FFmpeg出力を作成できません。OBSのログを確認してください。") return end
    obs.obs_output_set_media(output, obs.obs_get_video(), obs.obs_get_audio())
    obs.obs_output_set_mixer(output, track - 1)
    if not obs.obs_output_start(output) then
        report("error: " .. (obs.obs_output_get_last_error(output) or "共有出力の開始に失敗しました。"))
        stop_output()
    end
end

local function poll()
    local f = io.open(root .. "runtime/command.txt", "r")
    if f then
        local revision = f:read("*l")
        local action = f:read("*l")
        f:close()
        if revision and revision ~= last_revision then
            last_revision = revision
            if action == "start" then start_output() else stop_output() end
        end
    end
    if os.time() ~= last_status then
        last_status = os.time()
        if output then
            if obs.obs_output_active(output) then
                was_active = true
                report("active")
            elseif not was_active and os.time() < start_deadline then
                -- FFmpeg initializes on a background thread after obs_output_start returns.
                -- An inactive output during this window has not stopped yet.
                report("starting")
            else
                local err = obs.obs_output_get_last_error(output)
                if not err or err == "" then
                    err = was_active and "出力が停止しました。OBSのログを確認してください。" or "出力を開始できませんでした。OBSのログを確認してください。"
                end
                report("error: " .. err)
                stop_output()
            end
        else
            -- Preserve a recent error until a new command arrives.
            local s = io.open(root .. "runtime/obs-status.txt", "r")
            local msg = ""
            if s then s:read("*l"); msg = s:read("*l") or ""; s:close() end
            report(msg:sub(1, 6) == "error:" and msg or "idle")
        end
    end
end

function script_description()
    return "実況解説への本番映像共有。START.cmdを起動後、OBSのカスタムブラウザドックに管理URLを追加してください。音声トラック2は実況解説の声を除く設定を推奨します。"
end
function script_defaults(settings)
    obs.obs_data_set_default_string(settings, "ocs_folder", normalize_folder(script_path()))
    obs.obs_data_set_default_int(settings, "track", 2)
    obs.obs_data_set_default_int(settings, "quality", 0)
end
function script_properties()
    local p = obs.obs_properties_create()
    obs.obs_properties_add_path(p, "ocs_folder", "OCS接続フォルダ（START.cmdがある場所）", obs.OBS_PATH_DIRECTORY, nil, nil)
    obs.obs_properties_add_button(p, "connection_test", "接続テスト（スクリプトログに結果を表示）", connection_test)
    obs.obs_properties_add_int(p, "track", "共有する音声トラック", 1, 6, 1)
    local q = obs.obs_properties_add_list(p, "quality", "画質（変更後は共有を再開始）", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_INT)
    obs.obs_property_list_add_int(q, "標準 960×540 / 軽量", 0)
    obs.obs_property_list_add_int(q, "高画質 1280×720", 1)
    return p
end
function script_update(settings)
    local selected = obs.obs_data_get_string(settings, "ocs_folder")
    local next_root = normalize_folder(selected ~= "" and selected or script_path())
    if next_root ~= root then
        stop_output()
        root = next_root
        write_error_logged = false
        last_status = 0
        local f = io.open(root .. "runtime/command.txt", "r")
        last_revision = ""
        if f then last_revision = f:read("*l") or ""; f:close() end
        report("idle")
    end
    track = obs.obs_data_get_int(settings, "track")
    quality = obs.obs_data_get_int(settings, "quality")
end
function script_load(settings)
    script_update(settings)
    obs.script_log(obs.LOG_INFO, "OCS Lua v0.2.3 loaded / folder=" .. root)
    -- Ignore commands left by a previous server session.
    local f = io.open(root .. "runtime/command.txt", "r")
    if f then last_revision = f:read("*l") or ""; f:close() end
    report("idle")
    obs.timer_add(poll, 250)
end
function script_unload()
    obs.timer_remove(poll)
    stop_output()
    report("unloaded")
end
