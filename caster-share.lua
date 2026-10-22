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

-- A dedicated, unused mixer bit is the only bit OCS changes. Global mute/volume
-- and every streaming/recording track remain untouched.
local audio_bit = nil
local saved_sources = {}
local selected_sources = {}
local av = nil
local routing_error = ""
local function has(mask, bit) return math.floor(mask / bit) % 2 == 1 end
local function with_bit(mask, bit, enabled)
    if has(mask, bit) == enabled then return mask end
    return enabled and mask + bit or mask - bit
end
local function source_id(source)
    if obs.obs_source_get_uuid then return obs.obs_source_get_uuid(source) end
    return obs.obs_source_get_name(source)
end
local function used_tracks()
    if not obs.obs_frontend_get_profile_config or not obs.config_get_uint then
        return nil, "音源選択に必要なOBS APIがありません。OBSを更新してください。"
    end
    local cfg = obs.obs_frontend_get_profile_config()
    if not cfg then return nil, "OBSの出力設定を取得できません。" end
    if obs.config_get_bool and obs.config_get_bool(cfg, "Stream1", "EnableMultitrackVideo") then
        return 63, "拡張配信では音声トラックを動的に使うため、共有用トラックを確保できません。"
    end
    local used = 3 -- Reserve stream track 1 and VOD track 2, including simple mode.
    local function add_mask(mask)
        for i = 0, 5 do
            local b = 2 ^ i
            if has(mask, b) then used = with_bit(used, b, true) end
        end
    end
    local function add_track(index)
        if index >= 1 and index <= 6 then add_mask(2 ^ (index - 1)) end
    end
    local mode = obs.config_get_string(cfg, "Output", "Mode")
    if mode == "Advanced" then
        add_track(obs.config_get_uint(cfg, "AdvOut", "TrackIndex"))
        add_track(obs.config_get_uint(cfg, "AdvOut", "VodTrackIndex"))
        add_mask(obs.config_get_uint(cfg, "AdvOut", "RecTracks"))
        add_mask(obs.config_get_uint(cfg, "AdvOut", "FFAudioMixes"))
        add_mask(obs.config_get_uint(cfg, "AdvOut", "StreamMultiTrackAudioMixes"))
        add_track(obs.config_get_uint(cfg, "AdvOut", "FLVTrack"))
    else
        add_mask(obs.config_get_uint(cfg, "SimpleOutput", "RecTracks"))
    end
    -- Also protect actual encoders (including outputs initialized with different settings).
    for _, getter in ipairs({obs.obs_frontend_get_streaming_output,
                              obs.obs_frontend_get_recording_output,
                              obs.obs_frontend_get_replay_buffer_output}) do
        if getter then
            local o = getter()
            if o then
                for i = 0, 5 do
                    local e = obs.obs_output_get_audio_encoder(o, i)
                    if e then add_track(obs.obs_encoder_get_mixer_index(e) + 1) end
                end
                obs.obs_output_release(o)
            end
        end
    end
    return used, ""
end
local function available_track()
    local used, err = used_tracks()
    if not used or err ~= "" then return 0, err end
    for i = 6, 3, -1 do if not has(used, 2 ^ (i - 1)) then return i, "" end end
    return 0, "共有用の空き音声トラックがありません。OBSの録画設定でトラックを1つ空けてください。"
end
local function each_source(callback)
    local sources = obs.obs_enum_sources()
    if sources then
        for _, source in ipairs(sources) do
            if has(obs.obs_source_get_output_flags(source), obs.OBS_SOURCE_AUDIO) then callback(source) end
        end
        obs.source_list_release(sources)
    end
end
local function route_source(source)
    if not audio_bit or not has(obs.obs_source_get_output_flags(source), obs.OBS_SOURCE_AUDIO) then return end
    local id = source_id(source)
    local mask = obs.obs_source_get_audio_mixers(source)
    if not saved_sources[id] then
        saved_sources[id] = {name=obs.obs_source_get_name(source), original=has(mask, audio_bit)}
    end
    local next_mask = with_bit(mask, audio_bit, selected_sources[id] == true)
    if next_mask ~= mask then obs.obs_source_set_audio_mixers(source, next_mask) end
end
local function restore_audio()
    if audio_bit then
        each_source(function(source)
            local saved = saved_sources[source_id(source)]
            if saved then
                local current = obs.obs_source_get_audio_mixers(source)
                obs.obs_source_set_audio_mixers(source, with_bit(current, audio_bit, saved.original))
            end
        end)
    end
    audio_bit = nil
    saved_sources = {}
end
local function load_av()
    av = nil
    local file = io.open(root .. "runtime/av-settings.json", "r")
    if not file then return end
    local json = file:read("*a"); file:close()
    local data = obs.obs_data_create_from_json(json)
    if not data then routing_error = "映像・音声設定を読み込めません。管理画面で保存し直してください。"; return false end
    local height = obs.obs_data_get_int(data, "height")
    local fps = obs.obs_data_get_int(data, "fps")
    local bitrate = obs.obs_data_get_int(data, "bitrate")
    if (height ~= 540 and height ~= 720 and height ~= 1080) or (fps ~= 30 and fps ~= 60)
            or bitrate < 1500 or bitrate > 12000
            or obs.obs_data_get_string(data, "audio_mode") ~= "selected" then
        obs.obs_data_release(data)
        routing_error = "映像・音声設定が不正です。管理画面で保存し直してください。"; return false
    end
    av = {height=height, fps=fps, bitrate=bitrate}
    selected_sources = {}
    local array = obs.obs_data_get_array(data, "audio_sources")
    -- OBS data arrays contain objects; Python writes a companion object array.
    if array then obs.obs_data_array_release(array) end
    local list = obs.obs_data_get_array(data, "sources")
    if list then
        for i = 0, obs.obs_data_array_count(list) - 1 do
            local item = obs.obs_data_array_item(list, i)
            selected_sources[obs.obs_data_get_string(item, "id")] = true
            obs.obs_data_release(item)
        end
        obs.obs_data_array_release(list)
    end
    obs.obs_data_release(data)
    return true
end
local function details()
    if not obs.obs_enum_sources then return end
    local data = obs.obs_data_create()
    obs.obs_data_set_int(data, "timestamp", os.time())
    local free, err = available_track()
    obs.obs_data_set_int(data, "available_track", audio_bit and (math.log(audio_bit)/math.log(2) + 1) or free)
    obs.obs_data_set_string(data, "error", err)
    if obs.obs_video_info and obs.obs_get_video_info then
        local info = obs.obs_video_info()
        if obs.obs_get_video_info(info) then
            obs.obs_data_set_double(data, "native_fps", info.fps_num / info.fps_den)
            obs.obs_data_set_int(data, "native_width", info.output_width)
            obs.obs_data_set_int(data, "native_height", info.output_height)
        end
    end
    local array = obs.obs_data_array_create()
    each_source(function(source)
        local item = obs.obs_data_create()
        obs.obs_data_set_string(item, "id", source_id(source))
        obs.obs_data_set_string(item, "name", obs.obs_source_get_name(source))
        obs.obs_data_set_bool(item, "muted", obs.obs_source_muted(source))
        obs.obs_data_set_bool(item, "active", obs.obs_source_active(source))
        obs.obs_data_array_push_back(array, item)
        obs.obs_data_release(item)
    end)
    obs.obs_data_set_array(data, "sources", array)
    obs.obs_data_array_release(array)
    local file = io.open(root .. "runtime/obs-details.tmp", "w")
    if file then
        file:write(obs.obs_data_get_json(data)); file:close()
        -- Windows rename cannot replace an existing file; a brief missing file is harmless.
        os.remove(root .. "runtime/obs-details.json")
        os.rename(root .. "runtime/obs-details.tmp", root .. "runtime/obs-details.json")
    end
    obs.obs_data_release(data)
end
local function source_created(calldata)
    local source = obs.calldata_source(calldata, "source")
    if source then route_source(source) end
end

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
    restore_audio()
end

local function start_output(managed)
    stop_output()
    routing_error = ""
    av = nil
    if managed then
        if load_av() ~= true then
            routing_error = routing_error ~= "" and routing_error or "共有音源の設定ファイルがありません。管理画面で保存し直してください。"
            report("error: " .. routing_error); return
        end
    end
    if av then
        local free, err = available_track()
        if free == 0 then routing_error = err; report("error: " .. err); return end
        audio_bit = 2 ^ (free - 1)
        each_source(route_source)
    end
    was_active = false
    start_deadline = os.time() + 3
    local settings = obs.obs_data_create()
    obs.obs_data_set_string(settings, "url", "udp://127.0.0.1:18791?pkt_size=1316")
    obs.obs_data_set_string(settings, "format_name", "mpegts")
    obs.obs_data_set_string(settings, "video_encoder", "libx264")
    obs.obs_data_set_string(settings, "audio_encoder", "aac")
    obs.obs_data_set_string(settings, "video_settings", "preset=ultrafast tune=zerolatency pix_fmt=yuv420p")
    obs.obs_data_set_string(settings, "muxer_settings", "mpegts_flags=resend_headers flush_packets=1")
    obs.obs_data_set_int(settings, "video_bitrate", av and av.bitrate or (quality == 2 and 8000 or quality == 1 and 3500 or 1800))
    obs.obs_data_set_int(settings, "audio_bitrate", 128)
    obs.obs_data_set_int(settings, "gop_size", av and av.fps or 60)
    obs.obs_data_set_int(settings, "scale_width", av and math.floor(av.height * 16 / 9) or (quality == 2 and 1920 or quality == 1 and 1280 or 960))
    obs.obs_data_set_int(settings, "scale_height", av and av.height or (quality == 2 and 1080 or quality == 1 and 720 or 540))
    output = obs.obs_output_create("ffmpeg_output", "caster-share-output", settings, nil)
    obs.obs_data_release(settings)
    if not output then restore_audio(); report("error: FFmpeg出力を作成できません。OBSのログを確認してください。") return end
    obs.obs_output_set_media(output, obs.obs_get_video(), obs.obs_get_audio())
    obs.obs_output_set_mixer(output, audio_bit and (math.floor(math.log(audio_bit)/math.log(2) + 0.5)) or track - 1)
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
            if action == "start_av" then start_output(true)
            elseif action == "start" then start_output(false)
            else stop_output() end
        end
    end
    if audio_bit then
        local used, err = used_tracks()
        if not used or has(used, audio_bit) then
            stop_output()
            routing_error = err ~= "" and err or "共有用トラックがOBSの配信・録画で使われました。共有を停止しました。空きトラックを確保して再開始してください。"
            report("error: " .. routing_error)
        else
            each_source(route_source)
        end
    end
    if os.time() ~= last_status then
        details()
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
    return "実況解説への本番映像共有。START.cmdを起動後、OBSのカスタムブラウザドックに管理URLを追加してください。管理画面で1920×1080・最大60FPSと、共有に載せる音源を設定できます。音源未設定時は従来のトラックを使います。"
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
    obs.obs_property_list_add_int(q, "フルHD 1920×1080", 2)
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
    obs.script_log(obs.LOG_INFO, "OCS Lua v0.3.0 loaded / folder=" .. root)
    -- Ignore commands left by a previous server session.
    local f = io.open(root .. "runtime/command.txt", "r")
    if f then last_revision = f:read("*l") or ""; f:close() end
    report("idle")
    if obs.obs_get_signal_handler and obs.signal_handler_connect then
        obs.signal_handler_connect(obs.obs_get_signal_handler(), "source_create", source_created)
    end
    details()
    obs.timer_add(poll, 250)
end
function script_unload()
    obs.timer_remove(poll)
    if obs.obs_get_signal_handler and obs.signal_handler_disconnect then
        obs.signal_handler_disconnect(obs.obs_get_signal_handler(), "source_create", source_created)
    end
    stop_output()
    report("unloaded")
end
