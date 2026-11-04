"""
ACE shared-memory probe (telemetry phase 0 spike).

Standalone, standard library only, and read-only towards the game. It does not
talk to the server and is not part of the tray app. Run it on the Windows PC
while driving a few laps in Assetto Corsa EVO:

    python shm_probe.py                    # writes ace_probe_<timestamp>.csv
    python shm_probe.py --hz 50 --out my.csv
    python shm_probe.py --selftest         # struct sizes only; works anywhere

It answers the go/no-go questions for the telemetry plan:
  1. Do the struct layouts below match the running ACE build? (sizes,
     sm_version, sane values for speed/throttle/brake/position)
  2. How fast do the physics and graphics pages actually update?
  3. Do lap boundaries in shared memory (npos wrap, last_laptime_ms change)
     line up with the laps the tray detects from log.txt?
  4. Which slot of car_coordinates[60] is the player's car?

Struct definitions mirror Kunos' header as published in
github.com/dSyncro/acevo-shared-memory (src/bindings/source/wrapper.hpp),
which is compiled with #pragma pack(4). ctypes computes the offsets from the
field list, so nothing here is hand-offset.

Stop with Ctrl+C. A summary is printed and written next to the CSV.
"""

import argparse
import csv
import ctypes
import sys
import time
from ctypes import (
    c_bool, c_char, c_float, c_int8, c_int16, c_int32,
    c_uint8, c_uint16, c_uint32, c_uint64,
)
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Struct layouts (pack 4, little-endian, as in wrapper.hpp)
# ---------------------------------------------------------------------------

class _S(ctypes.Structure):
    _pack_ = 4


def _f4():
    return c_float * 4


class SPageFilePhysics(_S):
    _fields_ = [
        ("packetId", c_int32), ("gas", c_float), ("brake", c_float),
        ("fuel", c_float), ("gear", c_int32), ("rpms", c_int32),
        ("steerAngle", c_float), ("speedKmh", c_float),
        ("velocity", c_float * 3), ("accG", c_float * 3),
        ("wheelSlip", _f4()), ("wheelLoad", _f4()), ("wheelsPressure", _f4()),
        ("wheelAngularSpeed", _f4()), ("tyreWear", _f4()),
        ("tyreDirtyLevel", _f4()), ("tyreCoreTemperature", _f4()),
        ("camberRAD", _f4()), ("suspensionTravel", _f4()),
        ("drs", c_float), ("tc", c_float), ("heading", c_float),
        ("pitch", c_float), ("roll", c_float), ("cgHeight", c_float),
        ("carDamage", c_float * 5), ("numberOfTyresOut", c_int32),
        ("pitLimiterOn", c_int32), ("abs", c_float), ("kersCharge", c_float),
        ("kersInput", c_float), ("autoShifterOn", c_int32),
        ("rideHeight", c_float * 2), ("turboBoost", c_float),
        ("ballast", c_float), ("airDensity", c_float), ("airTemp", c_float),
        ("roadTemp", c_float), ("localAngularVel", c_float * 3),
        ("finalFF", c_float), ("performanceMeter", c_float),
        ("engineBrake", c_int32), ("ersRecoveryLevel", c_int32),
        ("ersPowerLevel", c_int32), ("ersHeatCharging", c_int32),
        ("ersIsCharging", c_int32), ("kersCurrentKJ", c_float),
        ("drsAvailable", c_int32), ("drsEnabled", c_int32),
        ("brakeTemp", _f4()), ("clutch", c_float),
        ("tyreTempI", _f4()), ("tyreTempM", _f4()), ("tyreTempO", _f4()),
        # --- ACE additions (offset 416) ---
        ("isAIControlled", c_int32),
        ("tyreContactPoint", (c_float * 3) * 4),
        ("tyreContactNormal", (c_float * 3) * 4),
        ("tyreContactHeading", (c_float * 3) * 4),
        ("brakeBias", c_float), ("localVelocity", c_float * 3),
        ("P2PActivations", c_int32), ("P2PStatus", c_int32),
        ("currentMaxRpm", c_int32),
        ("mz", _f4()), ("fx", _f4()), ("fy", _f4()),
        ("slipRatio", _f4()), ("slipAngle", _f4()),
        ("tcinAction", c_int32), ("absInAction", c_int32),
        ("suspensionDamage", _f4()), ("tyreTemp", _f4()),
        ("waterTemp", c_float), ("brakeTorque", _f4()),
        ("frontBrakeCompound", c_int32), ("rearBrakeCompound", c_int32),
        ("padLife", _f4()), ("discLife", _f4()),
        ("ignitionOn", c_int32), ("starterEngineOn", c_int32),
        ("isEngineRunning", c_int32), ("kerbVibration", c_float),
        ("slipVibrations", c_float), ("roadVibrations", c_float),
        ("absVibrations", c_float),
    ]


class SMEvoTyreState(_S):
    _fields_ = [
        ("slip", c_float), ("lock", c_bool), ("tyre_pression", c_float),
        ("tyre_temperature_c", c_float), ("brake_temperature_c", c_float),
        ("brake_pressure", c_float), ("tyre_temperature_left", c_float),
        ("tyre_temperature_center", c_float),
        ("tyre_temperature_right", c_float),
        ("tyre_compound_front", c_char * 33), ("tyre_compound_rear", c_char * 33),
        ("tyre_normalized_pressure", c_float),
        ("tyre_normalized_temperature_left", c_float),
        ("tyre_normalized_temperature_center", c_float),
        ("tyre_normalized_temperature_right", c_float),
        ("brake_normalized_temperature", c_float),
        ("tyre_normalized_temperature_core", c_float),
        ("place_holder", c_char * 128),
    ]


class SMEvoDamageState(_S):
    _fields_ = [(n, c_float) for n in (
        "damage_front", "damage_rear", "damage_left", "damage_right",
        "damage_center", "damage_suspension_lf", "damage_suspension_rf",
        "damage_suspension_lr", "damage_suspension_rr",
    )] + [("place_holder", c_char * 92)]


class SMEvoPitInfo(_S):
    _fields_ = [(n, c_int8) for n in (
        "damage", "fuel", "tyres_lf", "tyres_rf", "tyres_lr", "tyres_rr",
    )] + [("place_holder", c_char * 58)]


class SMEvoElectronics(_S):
    _fields_ = [
        ("tc_level", c_int8), ("tc_cut_level", c_int8), ("abs_level", c_int8),
        ("esc_level", c_int8), ("ebb_level", c_int8), ("brake_bias", c_float),
        ("engine_map_level", c_int8), ("turbo_level", c_float),
        ("ers_deployment_map", c_int8), ("ers_recharge_map", c_float),
        ("is_ers_heat_charging_on", c_bool), ("is_ers_overtake_mode_on", c_bool),
        ("is_drs_open", c_bool), ("diff_power_level", c_int8),
        ("diff_coast_level", c_int8), ("front_bump_damper_level", c_int8),
        ("front_rebound_damper_level", c_int8),
        ("rear_bump_damper_level", c_int8),
        ("rear_rebound_damper_level", c_int8), ("is_ignition_on", c_bool),
        ("is_pitlimiter_on", c_bool), ("active_performance_mode", c_int8),
        ("place_holder", c_char * 88),
    ]


class SMEvoInstrumentation(_S):
    _fields_ = [
        ("main_light_stage", c_int8), ("special_light_stage", c_int8),
        ("cockpit_light_stage", c_int8), ("wiper_level", c_int8),
        ("rain_lights", c_bool), ("direction_light_left", c_bool),
        ("direction_light_right", c_bool), ("flashing_lights", c_bool),
        ("warning_lights", c_bool), ("selected_display_index", c_int8),
        ("display_current_page_index", c_int8 * 16),
        ("are_headlights_visible", c_bool), ("place_holder", c_char * 101),
    ]


class SMEvoSessionState(_S):
    _fields_ = [
        ("phase_name", c_char * 33), ("time_left", c_char * 15),
        ("time_left_ms", c_int32), ("wait_time", c_char * 15),
        ("total_lap", c_int32), ("current_lap", c_int32),
        ("lights_on", c_int32), ("lights_mode", c_int32),
        ("lap_length_km", c_float), ("end_session_flag", c_int32),
        ("time_to_next_session", c_char * 15),
        ("disconnected_from_server", c_bool), ("restart_season_enabled", c_bool),
        ("ui_enable_drive", c_bool), ("ui_enable_setup", c_bool),
        ("is_ready_to_next_blinking", c_bool),
        ("show_waiting_for_players", c_bool), ("place_holder", c_char * 140),
    ]


class SMEvoTimingState(_S):
    _fields_ = [
        ("current_laptime", c_char * 15), ("delta_current", c_char * 15),
        ("delta_current_p", c_int32), ("last_laptime", c_char * 15),
        ("delta_last", c_char * 15), ("delta_last_p", c_int32),
        ("best_laptime", c_char * 15), ("ideal_laptime", c_char * 15),
        ("total_time", c_char * 15), ("is_invalid", c_bool),
        ("place_holder", c_char * 137),
    ]


class SMEvoAssistsState(_S):
    _fields_ = [
        ("auto_gear", c_uint8), ("auto_blip", c_uint8), ("auto_clutch", c_uint8),
        ("auto_clutch_on_start", c_uint8), ("manual_ignition_e_start", c_uint8),
        ("auto_pit_limiter", c_uint8), ("standing_start_assist", c_uint8),
        ("auto_steer", c_float), ("arcade_stability_control", c_float),
        ("place_holder", c_char * 48),
    ]


class SPageFileGraphicEvo(_S):
    _fields_ = [
        ("packetId", c_int32), ("status", c_int32),
        ("focused_car_id_a", c_uint64), ("focused_car_id_b", c_uint64),
        ("player_car_id_a", c_uint64), ("player_car_id_b", c_uint64),
        ("rpm", c_uint16),
    ] + [(n, c_bool) for n in (
        "is_rpm_limiter_on", "is_change_up_rpm", "is_change_down_rpm",
        "tc_active", "abs_active", "esc_active", "launch_active",
        "is_ignition_on", "is_engine_running", "kers_is_charging",
        "is_wrong_way", "is_drs_available", "battery_is_charging",
        "is_max_kj_per_lap_reached", "is_max_charge_kj_per_lap_reached",
    )] + [
        ("display_speed_kmh", c_int16), ("display_speed_mph", c_int16),
        ("display_speed_ms", c_int16), ("pitspeeding_delta", c_float),
        ("gear_int", c_int16),
    ] + [(n, c_float) for n in (
        "rpm_percent", "gas_percent", "brake_percent", "handbrake_percent",
        "clutch_percent", "steering_percent", "ffb_strength",
        "car_ffb_mupliplier", "water_temperature_percent",
        "water_pressure_bar", "fuel_pressure_bar",
    )] + [
        ("water_temperature_c", c_int8), ("air_temperature_c", c_int8),
    ] + [(n, c_float) for n in (
        "oil_temperature_c", "oil_pressure_bar", "exhaust_temperature_c",
        "g_forces_x", "g_forces_y", "g_forces_z", "turbo_boost",
        "turbo_boost_level", "turbo_boost_perc",
    )] + [
        ("steer_degrees", c_int32), ("current_km", c_float),
        ("total_km", c_uint32), ("total_driving_time_s", c_uint32),
        ("time_of_day_hours", c_int32), ("time_of_day_minutes", c_int32),
        ("time_of_day_seconds", c_int32), ("delta_time_ms", c_int32),
        ("current_lap_time_ms", c_int32), ("predicted_lap_time_ms", c_int32),
    ] + [(n, c_float) for n in (
        "fuel_liter_current_quantity", "fuel_liter_current_quantity_percent",
        "fuel_liter_per_km", "km_per_fuel_liter", "current_torque",
    )] + [
        ("current_bhp", c_int32),
        ("tyre_lf", SMEvoTyreState), ("tyre_rf", SMEvoTyreState),
        ("tyre_lr", SMEvoTyreState), ("tyre_rr", SMEvoTyreState),
        ("npos", c_float), ("kers_charge_perc", c_float),
        ("kers_current_perc", c_float), ("control_lock_time", c_float),
        ("car_damage", SMEvoDamageState), ("car_location", c_int32),
        ("pit_info", SMEvoPitInfo),
    ] + [(n, c_float) for n in (
        "fuel_liter_used", "fuel_liter_per_lap", "laps_possible_with_fuel",
        "battery_temperature", "battery_voltage",
        "instantaneous_fuel_liter_per_km", "instantaneous_km_per_fuel_liter",
        "gear_rpm_window",
    )] + [
        ("instrumentation", SMEvoInstrumentation),
        ("instrumentation_min_limit", SMEvoInstrumentation),
        ("instrumentation_max_limit", SMEvoInstrumentation),
        ("electronics", SMEvoElectronics),
        ("electronics_min_limit", SMEvoElectronics),
        ("electronics_max_limit", SMEvoElectronics),
        ("electronics_is_modifiable", SMEvoElectronics),
        ("total_lap_count", c_int32), ("current_pos", c_uint32),
        ("total_drivers", c_uint32), ("last_laptime_ms", c_int32),
        ("best_laptime_ms", c_int32), ("flag", c_int32),
        ("global_flag", c_int32), ("max_gears", c_uint32),
        ("engine_type", c_int32), ("has_kers", c_bool),
        ("is_last_lap", c_bool), ("performance_mode_name", c_char * 33),
        ("diff_coast_raw_value", c_float), ("diff_power_raw_value", c_float),
        ("race_cut_gained_time_ms", c_int32),
        ("distance_to_deadline", c_int32),
        ("race_cut_current_delta", c_float),
        ("session_state", SMEvoSessionState),
        ("timing_state", SMEvoTimingState),
    ] + [(n, c_int32) for n in (
        "player_ping", "player_latency", "player_cpu_usage",
        "player_cpu_usage_avg", "player_qos", "player_qos_avg",
        "player_fps", "player_fps_avg",
    )] + [
        ("driver_name", c_char * 33), ("driver_surname", c_char * 33),
        ("car_model", c_char * 33), ("is_in_pit_box", c_bool),
        ("is_in_pit_lane", c_bool), ("is_valid_lap", c_bool),
        ("car_coordinates", (c_float * 3) * 60),
        ("gap_ahead", c_float), ("gap_behind", c_float),
        ("active_cars", c_uint8), ("fuel_per_lap", c_float),
        ("fuel_estimated_laps", c_float),
        ("assists_state", SMEvoAssistsState),
        ("max_fuel", c_float), ("max_turbo_boost", c_float),
        ("use_single_compound", c_bool),
        ("car_ids", (c_uint64 * 2) * 60),
    ]


class SPageFileStaticEvo(_S):
    _fields_ = [
        ("sm_version", c_char * 15), ("ac_evo_version", c_char * 15),
        ("session", c_int32), ("session_name", c_char * 33),
        ("event_id", c_uint8), ("session_id", c_uint8),
        ("starting_grip", c_int32),
        ("starting_ambient_temperature_c", c_float),
        ("starting_ground_temperature_c", c_float),
        ("is_static_weather", c_bool), ("is_timed_race", c_bool),
        ("is_online", c_bool), ("number_of_sessions", c_int32),
        ("nation", c_char * 33), ("longitude", c_float), ("latitude", c_float),
        ("track", c_char * 33), ("track_configuration", c_char * 33),
        ("track_length_m", c_float),
    ]


MAPPINGS = {
    "physics": ("Local\\acevo_pmf_physics", SPageFilePhysics),
    "graphics": ("Local\\acevo_pmf_graphics", SPageFileGraphicEvo),
    "static": ("Local\\acevo_pmf_static", SPageFileStaticEvo),
}

STATUS = {0: "OFF", 1: "REPLAY", 2: "LIVE", 3: "PAUSE"}


def selftest() -> int:
    """Check the layouts against the sizes the header and docs assert."""
    expected = {
        SMEvoTyreState: 256, SMEvoDamageState: 128, SMEvoPitInfo: 64,
        SMEvoElectronics: 128, SMEvoInstrumentation: 128,
        SMEvoSessionState: 256, SMEvoTimingState: 256, SMEvoAssistsState: 64,
        SPageFilePhysics: 800, SPageFileStaticEvo: 208,
    }
    ok = True
    for cls, size in expected.items():
        got = ctypes.sizeof(cls)
        flag = "ok  " if got == size else "FAIL"
        ok &= got == size
        print(f"{flag} {cls.__name__:<22} {got:>5} bytes (expected {size})")
    # Offsets the docs state explicitly for the physics page.
    for name, off in (("speedKmh", 28), ("accG", 44), ("tyreTempO", 400),
                      ("isAIControlled", 416), ("absVibrations", 796)):
        got = getattr(SPageFilePhysics, name).offset
        flag = "ok  " if got == off else "FAIL"
        ok &= got == off
        print(f"{flag} physics.{name:<17} @ {got:>4} (expected {off})")
    print(f"info SPageFileGraphicEvo  {ctypes.sizeof(SPageFileGraphicEvo):>5} bytes"
          " (no size asserted upstream; npos @"
          f" {SPageFileGraphicEvo.npos.offset})")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Windows shared memory, opened read-only. mmap(tagname=...) is deliberately
# not used: it *creates* an empty mapping when the game is not running, which
# would make the probe read zeros and silently look like it works.
# ---------------------------------------------------------------------------

FILE_MAP_READ = 0x0004


class SharedPage:
    def __init__(self, name: str, cls):
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenFileMappingW.restype = ctypes.c_void_p
        k32.OpenFileMappingW.argtypes = [ctypes.c_uint32, c_bool, ctypes.c_wchar_p]
        k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                      ctypes.c_uint32, ctypes.c_uint32,
                                      ctypes.c_size_t]
        self._k32 = k32
        self.cls = cls
        self.size = ctypes.sizeof(cls)
        self.handle = k32.OpenFileMappingW(FILE_MAP_READ, False, name)
        if not self.handle:
            raise OSError(f"{name} not found — is ACE running and on track?")
        self.view = k32.MapViewOfFile(self.handle, FILE_MAP_READ, 0, 0, self.size)
        if not self.view:
            raise OSError(f"MapViewOfFile({name}) failed, "
                          f"error {ctypes.get_last_error()}")

    def read(self):
        """Copy the page out; retry if packetId moved during the copy."""
        for _ in range(3):
            snap = self.cls.from_buffer_copy(ctypes.string_at(self.view, self.size))
            after = c_int32.from_address(self.view).value
            if not hasattr(snap, "packetId") or snap.packetId == after:
                return snap
        return snap

    def close(self):
        self._k32.UnmapViewOfFile(ctypes.c_void_p(self.view))
        self._k32.CloseHandle(ctypes.c_void_p(self.handle))


def _s(b: bytes) -> str:
    return b.decode("utf-8", "replace")


def player_slot(gfx) -> int:
    pid = (gfx.player_car_id_a, gfx.player_car_id_b)
    for i in range(60):
        if (gfx.car_ids[i][0], gfx.car_ids[i][1]) == pid:
            return i
    return -1


COLUMNS = [
    "t_s", "phys_packet", "gfx_packet", "status", "npos", "current_km",
    "current_lap_time_ms", "last_laptime_ms", "best_laptime_ms",
    "total_lap_count", "session_current_lap", "is_valid_lap",
    "timing_is_invalid", "car_location", "speedKmh", "display_speed_kmh",
    "gas", "brake", "clutch", "steerAngle", "steering_percent",
    "steer_degrees", "gear", "gear_int", "rpms", "accG_0", "accG_1",
    "accG_2", "g_forces_x", "g_forces_y", "g_forces_z", "player_slot",
    "pos_x", "pos_y", "pos_z", "slot0_x", "slot0_y", "slot0_z",
]


def run(hz: float, out: Path) -> int:
    if sys.platform != "win32":
        print("The live probe needs Windows (ACE's shared memory). "
              "Use --selftest elsewhere.")
        return 2
    pages = {}
    try:
        for key, (name, cls) in MAPPINGS.items():
            pages[key] = SharedPage(name, cls)
    except OSError as exc:
        print(exc)
        return 1

    st = pages["static"].read()
    header = {
        "sm_version": _s(st.sm_version), "ac_evo_version": _s(st.ac_evo_version),
        "session": st.session, "session_name": _s(st.session_name),
        "track": _s(st.track), "track_configuration": _s(st.track_configuration),
        "track_length_m": round(st.track_length_m, 1),
    }
    for k, v in header.items():
        print(f"{k:>20}: {v}")
    print(f"Sampling at {hz:g} Hz into {out} — drive some laps, Ctrl+C to stop.\n")

    period = 1.0 / hz
    t0 = time.perf_counter()
    first_phys = first_gfx = None
    last_phys = last_gfx = None
    torn = 0
    prev_npos = None
    prev_last_lap = None
    lap_start_t = None
    laps = []
    rows = 0
    next_t = t0
    try:
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(COLUMNS)
            while True:
                now = time.perf_counter()
                t = now - t0
                ph = pages["physics"].read()
                gx = pages["graphics"].read()
                if first_phys is None:
                    first_phys, first_gfx = ph.packetId, gx.packetId
                if last_phys is not None and ph.packetId < last_phys:
                    torn += 1
                last_phys, last_gfx = ph.packetId, gx.packetId

                slot = player_slot(gx)
                pos = gx.car_coordinates[slot] if slot >= 0 else (0.0, 0.0, 0.0)
                w.writerow([
                    f"{t:.4f}", ph.packetId, gx.packetId,
                    STATUS.get(gx.status, gx.status), f"{gx.npos:.6f}",
                    f"{gx.current_km:.4f}", gx.current_lap_time_ms,
                    gx.last_laptime_ms, gx.best_laptime_ms, gx.total_lap_count,
                    gx.session_state.current_lap, int(gx.is_valid_lap),
                    int(gx.timing_state.is_invalid), gx.car_location,
                    f"{ph.speedKmh:.2f}", gx.display_speed_kmh,
                    f"{ph.gas:.3f}", f"{ph.brake:.3f}", f"{ph.clutch:.3f}",
                    f"{ph.steerAngle:.4f}", f"{gx.steering_percent:.4f}",
                    gx.steer_degrees, ph.gear, gx.gear_int, ph.rpms,
                    *(f"{v:.3f}" for v in ph.accG),
                    f"{gx.g_forces_x:.3f}", f"{gx.g_forces_y:.3f}",
                    f"{gx.g_forces_z:.3f}", slot,
                    *(f"{v:.2f}" for v in pos),
                    *(f"{v:.2f}" for v in gx.car_coordinates[0]),
                ])
                rows += 1

                # Lap boundary candidates, printed live so they can be held
                # against the tray's activity log.
                if prev_npos is not None and prev_npos > 0.9 and gx.npos < 0.1:
                    dur = (t - lap_start_t) if lap_start_t is not None else None
                    msg = (f"[{t:8.2f}s] npos wrapped; buffer "
                           f"{dur:.3f}s" if dur is not None else
                           f"[{t:8.2f}s] npos wrapped (first)")
                    print(msg)
                    if dur is not None:
                        laps.append(("npos_wrap", round(dur, 3)))
                    lap_start_t = t
                if prev_last_lap is not None and gx.last_laptime_ms != prev_last_lap:
                    print(f"[{t:8.2f}s] last_laptime_ms -> "
                          f"{gx.last_laptime_ms} ({gx.last_laptime_ms / 1000:.3f}s) "
                          f"valid={int(gx.is_valid_lap)} "
                          f"lap_count={gx.total_lap_count}")
                    laps.append(("last_laptime_ms", gx.last_laptime_ms / 1000))
                prev_npos, prev_last_lap = gx.npos, gx.last_laptime_ms

                next_t += period
                delay = next_t - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_t = time.perf_counter()
    except KeyboardInterrupt:
        pass
    finally:
        for p in pages.values():
            p.close()

    elapsed = time.perf_counter() - t0
    summary = [
        *(f"{k}: {v}" for k, v in header.items()),
        f"struct sizes: physics={ctypes.sizeof(SPageFilePhysics)} "
        f"graphics={ctypes.sizeof(SPageFileGraphicEvo)} "
        f"static={ctypes.sizeof(SPageFileStaticEvo)}",
        f"duration_s: {elapsed:.1f}",
        f"rows: {rows} ({rows / elapsed:.1f} Hz achieved)" if elapsed else "rows: 0",
    ]
    if elapsed and first_phys is not None:
        summary += [
            f"physics page rate: {(last_phys - first_phys) / elapsed:.1f} Hz",
            f"graphics page rate: {(last_gfx - first_gfx) / elapsed:.1f} Hz",
            f"physics packetId went backwards: {torn} times",
        ]
    summary += [f"lap event {kind}: {val}" for kind, val in laps]
    text = "\n".join(summary)
    print("\n" + text)
    out.with_suffix(".summary.txt").write_text(text + "\n", encoding="utf-8")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hz", type=float, default=50.0, help="sample rate (default 50)")
    ap.add_argument("--out", type=Path, default=None, help="CSV path")
    ap.add_argument("--selftest", action="store_true",
                    help="only check struct sizes (any OS)")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    out = args.out or Path(f"ace_probe_{datetime.now():%Y%m%d_%H%M%S}.csv")
    return run(max(1.0, min(args.hz, 333.0)), out)


if __name__ == "__main__":
    sys.exit(main())
