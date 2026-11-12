"""What gets synchronised.

MOTION: streamed every sim frame from the pilot flying (PF) to the other seat,
        whose aircraft is frozen and placed from this stream.
CONTROLS: also PF -> other, so yokes/levers move on both screens.
SWITCHES: two-way. Either pilot can flip them; the change is mirrored.

Switch entry: (key, simvar, unit, kind, arg, scale)
  kind "set"    -> write simvar directly (arg unused)
  kind "event"  -> send K event `arg` with value*scale
  kind "onoff"  -> arg = (ON_EVENT, OFF_EVENT)
  kind "toggle" -> send K event `arg` when states differ (skipped when the
                   input-event layer is active, to avoid double toggles)
"""

MOTION = [
    ("PLANE LATITUDE", "degrees"),
    ("PLANE LONGITUDE", "degrees"),
    ("PLANE ALTITUDE", "feet"),
    ("PLANE PITCH DEGREES", "degrees"),
    ("PLANE BANK DEGREES", "degrees"),
    ("PLANE HEADING DEGREES TRUE", "degrees"),
    ("VELOCITY BODY X", "feet per second"),
    ("VELOCITY BODY Y", "feet per second"),
    ("VELOCITY BODY Z", "feet per second"),
    ("ROTATION VELOCITY BODY X", "radians per second"),
    ("ROTATION VELOCITY BODY Y", "radians per second"),
    ("ROTATION VELOCITY BODY Z", "radians per second"),
]
M_LAT, M_LON, M_ALT, M_PITCH, M_BANK, M_HDG = range(6)
ANGLE_IDX = (M_PITCH, M_BANK, M_HDG)


# Read-only, every frame: world velocity (for curved interpolation) + ground elevation.
AUX = [
    ("VELOCITY WORLD X", "feet per second"),   # east
    ("VELOCITY WORLD Y", "feet per second"),   # up
    ("VELOCITY WORLD Z", "feet per second"),   # north
    ("GROUND ALTITUDE", "feet"),
]
A_VE, A_VU, A_VN, A_GROUND = range(4)

# Once a second: weather/sim-rate (mismatch warnings) + fuel/payload (copied at handoff).
ENV = [
    ("AMBIENT WIND DIRECTION", "degrees"),
    ("AMBIENT WIND VELOCITY", "knots"),
    ("SEA LEVEL PRESSURE", "millibars"),
    ("SIMULATION RATE", "number"),
    ("AMBIENT TEMPERATURE", "celsius"),
    ("AMBIENT VISIBILITY", "meters"),
    ("AMBIENT PRECIP STATE", "mask"),        # 2 none, 4 rain, 8 snow
]
E_TEMP, E_VIS, E_PRECIP = 4, 5, 6

# Read once a second by the cockpit module: engines running + crash detection.
def state_vars(engines):
    v = [(f"GENERAL ENG COMBUSTION:{i}", "bool") for i in range(1, max(1, min(4, engines)) + 1)]
    v.append(("REALISM CRASH DETECTION", "bool"))
    return v

# L:vars that are never synced (regexes, case-insensitive). Extend in lvar_ignore.txt.
LVAR_IGNORE = [
    r"^MobiFlight", r"^SharedCockpit", r"^YourControl",
    r"TIMER", r"_TIME$", r"ANIM", r"BLINK", r"FLASH", r"SOUND",
    r"^XMLVAR_.*_Anim", r"POTENTIOMETER_\d+_ANIM",
]
FUEL_TANKS = [f"FUEL TANK {t} QUANTITY" for t in (
    "LEFT MAIN", "RIGHT MAIN", "CENTER", "CENTER2", "CENTER3", "LEFT AUX", "RIGHT AUX",
    "LEFT TIP", "RIGHT TIP", "EXTERNAL1", "EXTERNAL2")]


def load_vars(payload_stations):
    v = [(n, "gallons") for n in FUEL_TANKS]
    v += [(f"PAYLOAD STATION WEIGHT:{i}", "pounds") for i in range(1, min(payload_stations, 20) + 1)]
    return v


def controls(engines):
    v = [
        ("ELEVATOR POSITION", "position"),
        ("AILERON POSITION", "position"),
        ("RUDDER POSITION", "position"),
        ("BRAKE LEFT POSITION", "position"),
        ("BRAKE RIGHT POSITION", "position"),
    ]
    for i in range(1, max(1, min(4, engines)) + 1):
        v += [
            (f"GENERAL ENG THROTTLE LEVER POSITION:{i}", "percent"),
            (f"GENERAL ENG MIXTURE LEVER POSITION:{i}", "percent"),
            (f"GENERAL ENG PROPELLER LEVER POSITION:{i}", "percent"),
        ]
    return v


def _light(key, var, ev):
    return (key, var, "bool", "event", ev, 1)


SWITCHES = [
    # gear / flaps / brakes
    ("gear", "GEAR HANDLE POSITION", "bool", "event", "GEAR_SET", 1),
    ("flaps", "FLAPS HANDLE INDEX", "number", "set", None, 1),
    ("spoilers", "SPOILERS HANDLE POSITION", "percent over 100", "event", "SPOILERS_SET", 16383),
    ("spoilers_arm", "SPOILERS ARMED", "bool", "event", "SPOILERS_ARM_SET", 1),
    ("park_brake", "BRAKE PARKING POSITION", "bool", "event", "PARKING_BRAKE_SET", 1),
    # trim
    ("elev_trim", "ELEVATOR TRIM PCT", "percent over 100", "event", "ELEVATOR_TRIM_SET", 16383),
    ("rudder_trim", "RUDDER TRIM PCT", "percent over 100", "event", "RUDDER_TRIM_SET", 16383),
    # lights
    _light("lt_landing", "LIGHT LANDING", "LANDING_LIGHTS_SET"),
    _light("lt_taxi", "LIGHT TAXI", "TAXI_LIGHTS_SET"),
    _light("lt_nav", "LIGHT NAV", "NAV_LIGHTS_SET"),
    _light("lt_beacon", "LIGHT BEACON", "BEACON_LIGHTS_SET"),
    _light("lt_strobe", "LIGHT STROBE", "STROBES_SET"),
    _light("lt_panel", "LIGHT PANEL", "PANEL_LIGHTS_SET"),
    _light("lt_recog", "LIGHT RECOGNITION", "RECOGNITION_LIGHTS_SET"),
    _light("lt_wing", "LIGHT WING", "WING_LIGHTS_SET"),
    _light("lt_logo", "LIGHT LOGO", "LOGO_LIGHTS_SET"),
    _light("lt_cabin", "LIGHT CABIN", "CABIN_LIGHTS_SET"),
    # electrical / anti-ice
    ("battery", "ELECTRICAL MASTER BATTERY", "bool", "toggle", "TOGGLE_MASTER_BATTERY", 1),
    ("alternator", "GENERAL ENG MASTER ALTERNATOR:1", "bool", "toggle", "TOGGLE_MASTER_ALTERNATOR", 1),
    ("avionics", "AVIONICS MASTER SWITCH", "bool", "event", "AVIONICS_MASTER_SET", 1),
    ("pitot", "PITOT HEAT", "bool", "event", "PITOT_HEAT_SET", 1),
    # radios (Hz so values are exact integers)
    ("com1", "COM ACTIVE FREQUENCY:1", "Hz", "event", "COM_RADIO_SET_HZ", 1),
    ("com1_sb", "COM STANDBY FREQUENCY:1", "Hz", "event", "COM_STBY_RADIO_SET_HZ", 1),
    ("com2", "COM ACTIVE FREQUENCY:2", "Hz", "event", "COM2_RADIO_SET_HZ", 1),
    ("com2_sb", "COM STANDBY FREQUENCY:2", "Hz", "event", "COM2_STBY_RADIO_SET_HZ", 1),
    ("nav1", "NAV ACTIVE FREQUENCY:1", "Hz", "event", "NAV1_RADIO_SET_HZ", 1),
    ("nav1_sb", "NAV STANDBY FREQUENCY:1", "Hz", "event", "NAV1_STBY_SET_HZ", 1),
    ("nav2", "NAV ACTIVE FREQUENCY:2", "Hz", "event", "NAV2_RADIO_SET_HZ", 1),
    ("nav2_sb", "NAV STANDBY FREQUENCY:2", "Hz", "event", "NAV2_STBY_SET_HZ", 1),
    ("obs1", "NAV OBS:1", "degrees", "event", "VOR1_SET", 1),
    ("obs2", "NAV OBS:2", "degrees", "event", "VOR2_SET", 1),
    ("xpdr", "TRANSPONDER CODE:1", "Bco16", "event", "XPNDR_SET", 1),
    ("baro", "KOHLSMAN SETTING MB:1", "millibars", "event", "KOHLSMAN_SET", 16),
    # autopilot
    ("ap_master", "AUTOPILOT MASTER", "bool", "onoff", ("AUTOPILOT_ON", "AUTOPILOT_OFF"), 1),
    ("ap_hdg", "AUTOPILOT HEADING LOCK", "bool", "onoff", ("AP_HDG_HOLD_ON", "AP_HDG_HOLD_OFF"), 1),
    ("ap_alt", "AUTOPILOT ALTITUDE LOCK", "bool", "onoff", ("AP_ALT_HOLD_ON", "AP_ALT_HOLD_OFF"), 1),
    ("ap_nav", "AUTOPILOT NAV1 LOCK", "bool", "onoff", ("AP_NAV1_HOLD_ON", "AP_NAV1_HOLD_OFF"), 1),
    ("ap_apr", "AUTOPILOT APPROACH HOLD", "bool", "onoff", ("AP_APR_HOLD_ON", "AP_APR_HOLD_OFF"), 1),
    ("ap_vs", "AUTOPILOT VERTICAL HOLD", "bool", "onoff", ("AP_VS_ON", "AP_VS_OFF"), 1),
    ("ap_flc", "AUTOPILOT FLIGHT LEVEL CHANGE", "bool", "onoff",
     ("FLIGHT_LEVEL_CHANGE_ON", "FLIGHT_LEVEL_CHANGE_OFF"), 1),
    ("ap_yd", "AUTOPILOT YAW DAMPER", "bool", "onoff", ("YAW_DAMPER_ON", "YAW_DAMPER_OFF"), 1),
    ("ap_fd", "AUTOPILOT FLIGHT DIRECTOR ACTIVE", "bool", "toggle", "TOGGLE_FLIGHT_DIRECTOR", 1),
    ("ap_hdg_bug", "AUTOPILOT HEADING LOCK DIR", "degrees", "event", "HEADING_BUG_SET", 1),
    ("ap_alt_sel", "AUTOPILOT ALTITUDE LOCK VAR", "feet", "event", "AP_ALT_VAR_SET_ENGLISH", 1),
    ("ap_vs_sel", "AUTOPILOT VERTICAL HOLD VAR", "feet/minute", "event", "AP_VS_VAR_SET_ENGLISH", 1),
    ("ap_spd_sel", "AUTOPILOT AIRSPEED HOLD VAR", "knots", "event", "AP_SPD_VAR_SET", 1),
]

# Tolerance when deciding a switch value really changed.
def tolerance(unit):
    if unit in ("percent over 100",):
        return 0.01
    if unit in ("degrees", "millibars", "knots"):
        return 0.4
    if unit in ("feet", "feet/minute"):
        return 5.0
    return 0.5  # bools, indexes, Hz, BCD

# Input events (MSFS 2024 B: vars) whose names contain these are continuous
# flight controls; they're carried by CONTROLS instead, so don't mirror them.
INPUT_EVENT_SKIP = ("YOKE", "PEDAL", "RUDDER_AXIS", "THROTTLE", "MIXTURE", "PROPELLER",
                    "CONDITION_LEVER", "TOE_BRAKE", "BRAKE_AXIS", "ELEVATOR_AXIS",
                    "AILERON_AXIS", "STICK", "COLLECTIVE", "CYCLIC", "TILLER", "CAMERA",
                    "VIEW")
