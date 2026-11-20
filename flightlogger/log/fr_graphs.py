"""Flight Review's graph set, ported from PX4/flight_review.

Source: app/plot_app/configured_plots.py, plotting.py, helper.py, config.py and
config_tables.py of https://github.com/PX4/flight_review (commit 80e0c0c, Nov 2026).
Copyright (c) 2016-2017, PX4 Pro Drone Autopilot. BSD-3-Clause, see
FLIGHT_REVIEW_LICENSE.md next to this file.

The data selection, derived values, titles, legends, colors and per-log
conditions follow the original line by line. Only the output changes: instead
of Bokeh figures, ``build_graphs`` returns data-only ``GraphDef`` objects that
the Qt graph panels draw.

Not ported (yet): the VTOL state band and tailsitter attitude conversion, the
Leaflet map, and decoding of 'event' messages (needs PX4's event definitions).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
import scipy.signal

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ulog_loader import FlightLog

# --- config.py / config_tables.py -------------------------------------------
colors8 = ['#d55e00', '#009e73', '#55b4e9', '#000000', '#e69f00', '#0072b2', '#cc79a7', '#f0e442']
colors3 = [colors8[0], colors8[1], colors8[2]]
colors2 = [colors8[0], colors8[1]]
color_gray = '#464646'
mission_setpoint_color = colors8[6]

flight_modes_table = {
    0: ('Manual', '#cc0000'), 1: ('Altitude', '#eecc00'), 8: ('Altitude Cruise', '#eecc00'),
    2: ('Position', '#00cc33'), 6: ('Position (Slow)', '#00cc33'), 10: ('Acro', '#66cc00'),
    14: ('Offboard', '#00cccc'), 15: ('Stabilized', '#0033cc'),
    3: ('Mission', '#6600cc'), 4: ('Loiter', '#6600cc'), 5: ('Return to Land', '#6600cc'),
    12: ('Descend', '#6600cc'), 13: ('Terminate', '#6600cc'), 17: ('Takeoff', '#6600cc'),
    18: ('Land', '#6600cc'), 19: ('Follow Target', '#6600cc'), 20: ('Precision Land', '#6600cc'),
    21: ('Orbit', '#6600cc'), 22: ('VTOL Takeoff', '#6600cc'),
    **{23 + i: (f'External {i + 1}', '#9700cc') for i in range(8)},
}
vtol_modes_table = {1: ('Transition', '#cc0000'), 2: ('Fixed-Wing', '#eecc00'),
                    3: ('Multicopter', '#0033cc')}

# helper.py: vehicle_gps_position names of vehicle_gnss receiver fields
_VEHICLE_GPS_POSITION_FIELDS = {
    'latitude': 'latitude_deg', 'longitude': 'longitude_deg', 'altitude_msl': 'altitude_msl_m',
    'altitude_ellipsoid': 'altitude_ellipsoid_m', 'speed_accuracy': 's_variance_m_s',
    'course_accuracy': 'c_variance_rad', 'noise': 'noise_per_ms', 'ground_speed': 'vel_m_s',
    'vel_north': 'vel_n_m_s', 'vel_east': 'vel_e_m_s', 'vel_down': 'vel_d_m_s', 'course': 'cog_rad',
}

_ERRORS = (KeyError, IndexError, ValueError, TypeError, ZeroDivisionError)


# --- output model --------------------------------------------------------------
@dataclass
class Series:
    label: str
    color: str
    t: np.ndarray          # seconds since log start (or Hz for FFT graphs)
    y: np.ndarray
    step: bool = False     # step line, value held until the next sample
    scatter: bool = False  # circles instead of a line


@dataclass
class Spectrogram:
    image: np.ndarray      # rows = frequency bins, columns = time bins, in dB
    t0: float
    t1: float
    f0: float
    f1: float


@dataclass
class GraphDef:
    id: str
    title: str
    y_label: str = ''
    kind: str = 'time'     # 'time' | 'spectrogram' | 'fft'
    x_label: str = ''
    series: list[Series] = field(default_factory=list)
    y_range: tuple[float, float] | None = None
    y_start: float | None = None
    hbands: list[tuple[float | None, float | None, str]] = field(default_factory=list)
    hlines: list[tuple[float, str]] = field(default_factory=list)
    vmarks: list[tuple[float, str]] = field(default_factory=list)   # FFT frequency marks
    nan_times: list[float] = field(default_factory=list)
    dropouts: list[tuple[float, float, float, float]] = field(default_factory=list)
    spectrogram: Spectrogram | None = None
    mode_background: bool = True

    @property
    def time_based(self) -> bool:
        return self.kind in ('time', 'spectrogram')


@dataclass
class TrackDef:
    """Local position track (Flight Review's first, 2D plot). x = East, y = North."""
    series: list[Series]
    t: np.ndarray          # time of the estimated track samples
    east: np.ndarray
    north: np.ndarray


def slug(title: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', title.lower()).strip('_')


# --- data access with Flight Review's compatibility fixes ------------------------
class _Ctx:
    def __init__(self, fl: FlightLog):
        self.fl = fl
        self.ulog = fl.ulog
        self.virtual: dict[tuple[str, int], dict] = {}
        self._cache: dict[tuple[str, int], dict] = {}

    def has(self, topic: str) -> bool:
        return bool(self.fl.instances(topic)) or any(k[0] == topic for k in self.virtual)

    def ds(self, topic: str, instance: int = 0) -> dict | None:
        key = (topic, instance)
        if key in self.virtual:
            return self.virtual[key]
        if key in self._cache:
            return self._cache[key]
        if not self.fl.has(topic, instance):
            return None
        d = dict(self.fl.dataset(topic, instance).data)
        # COMPATIBILITY renames (configured_plots.py)
        if topic == 'system_power':
            for old, new in (('voltage5V_v', 'voltage5v_v'), ('voltage3V3_v', 'sensors3v3[0]'),
                             ('voltage3v3_v', 'sensors3v3[0]')):
                if old in d:
                    d[new] = d.pop(old)
        elif topic == 'tecs_status' and 'airspeed_sp' in d:
            d['true_airspeed_sp'] = d.pop('airspeed_sp')
        # PX4ULog.add_roll_pitch_yaw()
        for name, suffix in (('vehicle_attitude', ''), ('vehicle_vision_attitude', ''),
                             ('vehicle_attitude_groundtruth', ''), ('vehicle_attitude_setpoint', '_d')):
            if topic == name and f'q{suffix}[0]' in d:
                q = [d[f'q{suffix}[{i}]'].astype(np.float64) for i in range(4)]
                d['roll' + suffix] = np.arctan2(2 * (q[0] * q[1] + q[2] * q[3]),
                                                1 - 2 * (q[1] * q[1] + q[2] * q[2]))
                d['pitch' + suffix] = np.arcsin(np.clip(2 * (q[0] * q[2] - q[3] * q[1]), -1, 1))
                d['yaw' + suffix] = np.arctan2(2 * (q[0] * q[3] + q[1] * q[2]),
                                               1 - 2 * (q[2] * q[2] + q[3] * q[3]))
        self._cache[key] = d
        return d

    def sec(self, ts) -> np.ndarray:
        return (np.asarray(ts).astype(np.int64) - np.int64(self.fl.t0_us)) / 1e6

    def add_virtual_fifo_topic_data(self, topic_name: str, instance: int = 0) -> bool:
        """plotting.add_virtual_fifo_topic_data, vectorized."""
        d = self.ds(topic_name, instance)
        if d is None:
            return False
        try:
            t = d['timestamp_sample'].astype(np.int64)
            dt = d['dt'].astype(np.float64)
            samples = d['samples'].astype(np.int64)
            scale = d['scale'].astype(np.float64)
            n_fields = 0
            while f'x[{n_fields}]' in d:
                n_fields += 1
            if n_fields == 0 or len(t) == 0:
                return False
            idx = np.arange(n_fields)[None, :]
            mask = idx < samples[:, None]
            t_new = (t[:, None] - (samples[:, None] - idx - 1) * dt[:, None])[mask]
            out = {'timestamp': t_new, 'timestamp_sample': t_new}
            for axis in 'xyz':
                arr = np.stack([d[f'{axis}[{s}]'] for s in range(n_fields)], axis=1).astype(np.float64)
                out[axis] = (arr * scale[:, None])[mask]
        except _ERRORS:
            return False
        self.virtual[(topic_name + '_virtual', instance)] = out
        return True


class GnssTopic:
    def __init__(self, ctx: _Ctx):
        self.name = 'vehicle_gnss' if ctx.has('vehicle_gnss') else 'vehicle_gps_position'

    def field(self, name: str) -> str:
        if self.name == 'vehicle_gps_position':
            return _VEHICLE_GPS_POSITION_FIELDS.get(name, name)
        return 'receiver.' + name


class ActuatorControls:
    """helper.ActuatorControls"""

    def __init__(self, ctx: _Ctx, use_dynamic_control_alloc: bool, instance: int = 0):
        self.thrust = self.thrust_x = self.thrust_z_neg = None
        if use_dynamic_control_alloc:
            self.topic_instance = instance
            self.torque_sp_topic = 'vehicle_torque_setpoint'
            self.thrust_sp_topic = 'vehicle_thrust_setpoint'
            self.torque_axes_field_names = ['xyz[0]', 'xyz[1]', 'xyz[2]']
            try:
                sp = ctx.ds('vehicle_thrust_setpoint', 0)
                x, y, z = (sp[f'xyz[{i}]'].astype(np.float64) for i in range(3))
                self.thrust = np.sqrt(x ** 2 + y ** 2 + z ** 2)
                self.thrust_x = x
                self.thrust_z_neg = -z
                if instance != 0:
                    inst = ctx.ds('vehicle_thrust_setpoint', instance)
                    ts, ti = sp['timestamp'].astype(np.float64), inst['timestamp'].astype(np.float64)
                    self.thrust = np.interp(ti, ts, self.thrust)
                    self.thrust_x = np.interp(ti, ts, self.thrust_x)
                    self.thrust_z_neg = np.interp(ti, ts, self.thrust_z_neg)
            except (*_ERRORS, AttributeError):
                self.thrust = None
        else:
            self.topic_instance = 0
            self.torque_sp_topic = f'actuator_controls_{instance}'
            self.thrust_sp_topic = f'actuator_controls_{instance}'
            self.torque_axes_field_names = ['control[0]', 'control[1]', 'control[2]']
            try:
                sp = ctx.ds(self.torque_sp_topic)
                self.thrust = sp['control[3]']
                if instance == 0:
                    self.thrust_z_neg = sp['control[3]']
                else:
                    self.thrust_x = sp['control[3]']
            except (*_ERRORS, AttributeError):
                self.thrust = None


# --- DataPlot equivalent ------------------------------------------------------------
class _Plot:
    """Mirrors plotting.DataPlot's API and error semantics."""

    def __init__(self, ctx: _Ctx, data_name: str, title: str, y_axis_label: str = '',
                 y_range=None, y_start=None, topic_instance: int = 0, kind: str = 'time',
                 x_axis_label: str = '', modes: bool = True):
        self.ctx = ctx
        self.g = GraphDef(slug(title), title, y_axis_label, kind, x_axis_label,
                          y_range=y_range, y_start=y_start,
                          mode_background=modes and kind == 'time')
        self._had_error = False
        self._previous_success = False
        self.dataset = ctx.ds(data_name, topic_instance)
        if self.dataset is None:
            self._had_error = True

    @property
    def had_error(self):
        return self._had_error

    def change_dataset(self, data_name: str, topic_instance: int = 0):
        if not self._had_error:
            self._previous_success = True
        self._had_error = False
        self.dataset = self.ctx.ds(data_name, topic_instance)
        if self.dataset is None:
            self._had_error = True

    def _expand(self, field_names):
        out = []
        n = len(self.dataset['timestamp'])
        for f in field_names:
            if callable(f):
                _, data = f(self.dataset)
            else:
                data = self.dataset[f]
            data = np.asarray(data, dtype=np.float64)
            if data.shape != (n,):
                raise ValueError('length mismatch')
            out.append(data)
        return out

    def add_graph(self, field_names, colors, legends, mark_nan=False, use_step_lines=False):
        if self._had_error:
            return
        try:
            t = self.ctx.sec(self.dataset['timestamp'])
            ys = self._expand(field_names)
            if mark_nan:
                nan_t = set(self.g.nan_times)
                for y in ys:
                    nan = np.isnan(y)
                    starts = nan & ~np.concatenate(([False], nan[:-1]))
                    nan_t.update(t[starts].tolist())
                self.g.nan_times = sorted(nan_t)
            for y, c, legend in zip(ys, colors, legends):
                self.g.series.append(Series(legend, c, t, y, step=use_step_lines))
        except _ERRORS:
            self._had_error = True

    def add_circle(self, field_names, colors, legends):
        if self._had_error:
            return
        try:
            t = self.ctx.sec(self.dataset['timestamp'])
            for y, c, legend in zip(self._expand(field_names), colors, legends):
                self.g.series.append(Series(legend, c, t, y, scatter=True))
        except _ERRORS:
            self._had_error = True

    def add_span(self, field_name, line_color='black'):
        if self._had_error:
            return
        try:
            value = float(np.mean(self.dataset[field_name]))
            if not np.isnan(value):
                self.g.hlines.append((value, line_color))
        except _ERRORS:
            self._had_error = True

    def add_horizontal_background_boxes(self, colors, limits):
        bottom = None
        for color, top in zip(colors, list(limits) + [None]):
            self.g.hbands.append((bottom, top, color))
            bottom = top

    def add_dropouts(self, dropouts, min_value):
        for d in dropouts:
            t0 = (d.timestamp - self.ctx.fl.t0_us) / 1e6
            self.g.dropouts.append((t0, t0 + d.duration / 1000, min_value,
                                    min_value + d.duration * 1000))

    # DataPlotSpec.add_graph
    def add_spectrogram(self, field_names, legends, window='hann', window_length=256, noverlap=128):
        if self._had_error:
            return
        try:
            key = 'timestamp_sample' if 'timestamp_sample' in self.dataset else 'timestamp'
            ts = self.dataset[key].astype(np.int64)
            dt_diff = np.diff(ts)
            delta_t = np.median(dt_diff) * 1e-6
            mean_delta_t = np.mean(dt_diff) * 1e-6
            if delta_t < 1e-6:
                self._had_error = True
                return
            fs = int(1.0 / delta_t)
            if fs < 100:
                self._had_error = True
                return
            sum_psd = None
            for data in self._expand(field_names):
                frequency, time, psd = scipy.signal.spectrogram(
                    data, fs=fs, window=window, nperseg=window_length, noverlap=noverlap,
                    scaling='density')
                sum_psd = psd if sum_psd is None else sum_psd + psd
            time = time * (mean_delta_t / delta_t) + (ts[0] - self.ctx.fl.t0_us) / 1e6
            with np.errstate(divide='ignore'):
                image = 10 * np.log10(sum_psd)
            if np.isneginf(image).any():
                image[np.isneginf(image)] = np.min(np.ma.masked_invalid(image))
            max_cols = 2000
            if len(time) > max_cols:
                step = int(len(time) / max_cols)
                time, image = time[::step], image[:, ::step]
            self.g.title += ''.join(' ' + legend for legend in legends) + ' [dB]'
            self.g.spectrogram = Spectrogram(image, float(time[0]), float(time[-1]),
                                             float(frequency[0]), float(frequency[-1]))
            self.g.y_range = (float(frequency[0]), float(frequency[-1]))
        except _ERRORS:
            self._had_error = True

    # DataPlotFFT.add_graph
    def add_fft(self, field_names, colors, legends):
        if self._had_error:
            return
        try:
            key = 'timestamp_sample' if 'timestamp_sample' in self.dataset else 'timestamp'
            ts = self.dataset[key]
            n = len(ts)
            delta_t = np.median(np.diff(ts.astype(np.int64))) * 1e-6
            fs = 1.0 / delta_t if delta_t > 0 else float('inf')
            if fs < 100 or fs == float('inf'):
                self._had_error = True
                return
            freqs = np.fft.fftfreq(n, delta_t)
            mean_start_freq = 40
            for data, color, legend in zip(self._expand(field_names), colors, legends):
                fft_values = 2 / n * np.abs(np.fft.fft(data))
                mean_v = float(np.mean(fft_values[freqs >= mean_start_freq]))
                legend = f'{legend} (mean above {mean_start_freq} Hz: {mean_v:.2f})'
                half = n // 2
                self.g.series.append(Series(legend, color, freqs[:half], fft_values[:half]))
                self.g.series.append(Series('', color, np.array([mean_start_freq, freqs.max()]),
                                            np.array([mean_v, mean_v])))
        except _ERRORS:
            self._had_error = True

    def mark_frequency(self, frequency, label):
        self.g.vmarks.append((float(frequency), label))

    def finalize(self) -> GraphDef | None:
        if self._had_error and not self._previous_success:
            return None
        if not self.g.series and self.g.spectrogram is None:
            return None
        return self.g


def _np_max(a) -> float:
    return float(np.nanmax(a)) if len(a) else float('nan')


# --- configured_plots.generate_plots ----------------------------------------------
def build_graphs(fl: FlightLog) -> list[GraphDef]:
    ctx = _Ctx(fl)
    ulog = ctx.ulog
    params = ulog.initial_parameters
    plots: list[GraphDef] = []

    def add(p: _Plot):
        g = p.finalize()
        if g is not None:
            plots.append(g)

    if ctx.has('vehicle_air_data') or ctx.has('vehicle_magnetometer'):
        baro_alt_meter_topic, magnetometer_ga_topic = 'vehicle_air_data', 'vehicle_magnetometer'
    else:
        baro_alt_meter_topic = magnetometer_ga_topic = 'sensor_combined'
    manual_control_sp_controls = ['roll', 'pitch', 'yaw', 'throttle']
    manual_control_sp_throttle_range = '[-1, 1]'
    mcs = ctx.ds('manual_control_setpoint')
    if mcs is not None and 'throttle' not in mcs:
        manual_control_sp_controls = ['y', 'x', 'r', 'z']
        manual_control_sp_throttle_range = '[0, 1]'
    gnss = GnssTopic(ctx)
    gnss_altitude = None
    gd = ctx.ds(gnss.name)
    if gd is not None:
        try:
            if int(ulog.msg_info_dict.get('ver_data_format', 0)) >= 2:
                gnss_altitude = gd[gnss.field('altitude_msl')]
            else:
                gnss_altitude = gd['alt'] * 0.001
        except KeyError:
            gnss_altitude = None

    if ctx.has('vehicle_angular_velocity'):
        rate_estimated_topic_name = 'vehicle_angular_velocity'
        rate_groundtruth_topic_name = 'vehicle_angular_velocity_groundtruth'
        rate_field_names = ['xyz[0]', 'xyz[1]', 'xyz[2]']
    else:
        rate_estimated_topic_name = 'vehicle_attitude'
        rate_groundtruth_topic_name = 'vehicle_attitude_groundtruth'
        rate_field_names = ['rollspeed', 'pitchspeed', 'yawspeed']
    manual_control_switches_topic = ('manual_control_switches' if ctx.has('manual_control_switches')
                                     else 'manual_control_setpoint')
    dynamic_control_alloc = ctx.has('actuator_motors') or ctx.has('actuator_servos')
    actuator_controls_0 = ActuatorControls(ctx, dynamic_control_alloc, 0)
    actuator_controls_1 = ActuatorControls(ctx, dynamic_control_alloc, 1)

    is_vtol = False
    vs = ctx.ds('vehicle_status')
    if vs is not None and 'is_vtol' in vs and len(vs['is_vtol']):
        is_vtol = bool(np.amax(vs['is_vtol']) == 1)

    # Altitude estimate
    p = _Plot(ctx, gnss.name, 'Altitude Estimate', '[m]')
    p.add_graph([lambda d: ('alt', gnss_altitude)], colors8[0:1], ['GPS Altitude (MSL)'])
    p.change_dataset(baro_alt_meter_topic)
    p.add_graph(['baro_alt_meter'], colors8[1:2], ['Barometer Altitude'])
    p.change_dataset('vehicle_global_position')
    p.add_graph(['alt'], colors8[2:3], ['Fused Altitude Estimation'])
    p.change_dataset('position_setpoint_triplet')
    p.add_circle(['current.alt'], [mission_setpoint_color], ['Altitude Setpoint'])
    add(p)

    # Roll/Pitch/Yaw angle & angular rate
    for index, axis in enumerate(['roll', 'pitch', 'yaw']):
        axis_name = axis.capitalize()
        p = _Plot(ctx, 'vehicle_attitude', axis_name + ' Angle', '[deg]')
        p.add_graph([lambda d: (axis, np.rad2deg(d[axis]))], colors3[0:1],
                    [axis_name + ' Estimated'], mark_nan=True)
        p.change_dataset('vehicle_attitude_setpoint')
        p.add_graph([lambda d: (axis + '_d', np.rad2deg(d[axis + '_d']))], colors3[1:2],
                    [axis_name + ' Setpoint'], use_step_lines=True)
        if axis == 'yaw':
            p.add_graph([lambda d: ('yaw_sp_move_rate', np.rad2deg(d['yaw_sp_move_rate']))],
                        colors3[2:3], [axis_name + ' FF Setpoint [deg/s]'], use_step_lines=True)
        p.change_dataset('vehicle_attitude_groundtruth')
        p.add_graph([lambda d: (axis, np.rad2deg(d[axis]))], [color_gray],
                    [axis_name + ' Groundtruth'])
        add(p)

        p = _Plot(ctx, rate_estimated_topic_name, axis_name + ' Angular Rate', '[deg/s]')
        p.add_graph([lambda d: (axis + 'speed', np.rad2deg(d[rate_field_names[index]]))],
                    colors3[0:1], [axis_name + ' Rate Estimated'], mark_nan=True)
        p.change_dataset('vehicle_rates_setpoint')
        p.add_graph([lambda d: (axis, np.rad2deg(d[axis]))], colors3[1:2],
                    [axis_name + ' Rate Setpoint'], mark_nan=True, use_step_lines=True)
        rate_int_limit = '(*100)'
        lim_param = 'MC_' + axis[0].upper() + 'R_INT_LIM'
        if lim_param in params:
            rate_int_limit = '[-{0:.0f}, {0:.0f}]'.format(params[lim_param] * 100)
        p.change_dataset('rate_ctrl_status')
        p.add_graph([lambda d: (axis, d[axis + 'speed_integ'] * 100)], colors3[2:3],
                    [axis_name + ' Rate Integral ' + rate_int_limit])
        p.change_dataset(rate_groundtruth_topic_name)
        p.add_graph([lambda d: (axis + 'speed', np.rad2deg(d[rate_field_names[index]]))],
                    [color_gray], [axis_name + ' Rate Groundtruth'])
        add(p)

    # Local position
    for axis in ['x', 'y', 'z']:
        p = _Plot(ctx, 'vehicle_local_position', 'Local Position ' + axis.upper(), '[m]')
        p.add_graph([axis], colors2[0:1], [axis.upper() + ' Estimated'], mark_nan=True)
        p.change_dataset('vehicle_local_position_setpoint')
        p.add_graph([axis], colors2[1:2], [axis.upper() + ' Setpoint'], use_step_lines=True)
        add(p)

    # Velocity
    p = _Plot(ctx, 'vehicle_local_position', 'Velocity', '[m/s]')
    p.add_graph(['vx', 'vy', 'vz'], colors8[0:3], ['X', 'Y', 'Z'])
    p.change_dataset('vehicle_local_position_setpoint')
    p.add_graph(['vx', 'vy', 'vz'], [colors8[5], colors8[4], colors8[6]],
                ['X Setpoint', 'Y Setpoint', 'Z Setpoint'], use_step_lines=True)
    add(p)

    # Visual Odometry
    if ctx.has('vehicle_visual_odometry'):
        p = _Plot(ctx, 'vehicle_visual_odometry', 'Visual Odometry Position', '[m]')
        p.add_graph(['x', 'y', 'z'], colors3, ['X', 'Y', 'Z'], mark_nan=True)
        p.change_dataset('vehicle_local_position_groundtruth')
        p.add_graph(['x', 'y', 'z'], colors8[2:5], ['Groundtruth X', 'Groundtruth Y', 'Groundtruth Z'])
        add(p)

        p = _Plot(ctx, 'vehicle_visual_odometry', 'Visual Odometry Velocity', '[m]')
        p.add_graph(['vx', 'vy', 'vz'], colors3, ['X', 'Y', 'Z'], mark_nan=True)
        p.change_dataset('vehicle_local_position_groundtruth')
        p.add_graph(['vx', 'vy', 'vz'], colors8[2:5],
                    ['Groundtruth VX', 'Groundtruth VY', 'Groundtruth VZ'])
        add(p)

        p = _Plot(ctx, 'vehicle_visual_odometry', 'Visual Odometry Attitude', '[deg]')
        p.add_graph([lambda d: ('roll', np.rad2deg(d['roll'])),
                     lambda d: ('pitch', np.rad2deg(d['pitch'])),
                     lambda d: ('yaw', np.rad2deg(d['yaw']))],
                    colors3, ['Roll', 'Pitch', 'Yaw'], mark_nan=True)
        p.change_dataset('vehicle_attitude_groundtruth')
        p.add_graph([lambda d: ('roll', np.rad2deg(d['roll'])),
                     lambda d: ('pitch', np.rad2deg(d['pitch'])),
                     lambda d: ('yaw', np.rad2deg(d['yaw']))],
                    colors8[2:5], ['Roll Groundtruth', 'Pitch Groundtruth', 'Yaw Groundtruth'])
        add(p)

        p = _Plot(ctx, 'vehicle_visual_odometry', 'Visual Odometry Attitude Rate', '[deg]')
        p.add_graph([lambda d: ('rollspeed', np.rad2deg(d['rollspeed'])),
                     lambda d: ('pitchspeed', np.rad2deg(d['pitchspeed'])),
                     lambda d: ('yawspeed', np.rad2deg(d['yawspeed']))],
                    colors3, ['Roll Rate', 'Pitch Rate', 'Yaw Rate'], mark_nan=True)
        p.change_dataset(rate_groundtruth_topic_name)
        p.add_graph([lambda d, i=i: (n, np.rad2deg(d[rate_field_names[i]]))
                     for i, n in enumerate(['rollspeed', 'pitchspeed', 'yawspeed'])],
                    colors8[2:5],
                    ['Roll Rate Groundtruth', 'Pitch Rate Groundtruth', 'Yaw Rate Groundtruth'])
        add(p)

        p = _Plot(ctx, 'vehicle_visual_odometry', 'Visual Odometry Latency', '[ms]')
        p.add_graph([lambda d: ('latency', 1e-3 * (d['timestamp'].astype(np.float64)
                                                   - d['timestamp_sample'].astype(np.float64)))],
                    colors3, ['VIO Latency'], mark_nan=True)
        add(p)

    # Airspeed vs ground speed (only with airspeed data or a VTOL)
    if is_vtol or ctx.has('airspeed'):
        p = _Plot(ctx, 'vehicle_global_position', 'Airspeed', '[m/s]')
        p.add_graph([lambda d: ('groundspeed_estimated', np.sqrt(d['vel_n'] ** 2 + d['vel_e'] ** 2))],
                    colors8[0:1], ['Ground Speed Estimated'])
        av = ctx.ds('airspeed_validated')
        if av is not None:
            p.change_dataset('airspeed_validated')
            if 'airspeed_sensor_measurement_valid' in av and \
                    np.amax(av['airspeed_sensor_measurement_valid']) == 1:
                p.add_graph(['true_airspeed_m_s'], colors8[1:2], ['True Airspeed'])
            else:
                p.add_graph(['true_ground_minus_wind_m_s'], colors8[1:2],
                            ['True Airspeed (estimated)'])
        else:
            p.change_dataset('airspeed')
            p.add_graph(['indicated_airspeed_m_s'], colors8[1:2], ['Indicated Airspeed'])
        p.change_dataset(gnss.name)
        p.add_graph([gnss.field('ground_speed')], colors8[2:3], ['Ground Speed (from GPS)'])
        p.change_dataset('tecs_status')
        p.add_graph(['true_airspeed_sp'], colors8[3:4], ['True Airspeed Setpoint'])
        add(p)

    # TECS
    p = _Plot(ctx, 'tecs_status', 'TECS', '[m/s]', y_start=0)
    p.add_graph(['height_rate', 'height_rate_setpoint'], colors2,
                ['Height Rate', 'Height Rate Setpoint'], mark_nan=True)
    add(p)

    # Manual control inputs
    if ctx.has('manual_control_setpoint'):
        p = _Plot(ctx, 'manual_control_setpoint', 'Manual Control Inputs (Radio or Joystick)',
                  y_range=(-1.1, 1.1))
        p.add_graph(manual_control_sp_controls + ['aux1', 'aux2'], colors8[0:6],
                    ['Y / Roll', 'X / Pitch', 'Yaw', 'Throttle ' + manual_control_sp_throttle_range,
                     'Aux1', 'Aux2'])
        p.change_dataset(manual_control_switches_topic)
        p.add_graph([lambda d: ('mode_slot', d['mode_slot'] / 6),
                     lambda d: ('kill_switch', d['kill_switch'] == 1)],
                    colors8[6:8], ['Flight Mode', 'Kill Switch'])
        add(p)
    else:
        p = _Plot(ctx, 'rc_channels', 'Raw Radio Control Inputs', y_range=(-1.1, 1.1))
        n_ch = 8
        if p.dataset is not None:
            n_ch = int(min(np.amax(p.dataset['channel_count']), n_ch))
        p.add_graph([f'channels[{i}]' for i in range(n_ch)], colors8[0:n_ch],
                    [f'Channel {i}' for i in range(n_ch)], mark_nan=True)
        add(p)

    # Actuator controls 0
    p = _Plot(ctx, actuator_controls_0.torque_sp_topic, 'Actuator Controls', y_start=0)
    p.add_graph(actuator_controls_0.torque_axes_field_names, colors8[0:3],
                ['Roll', 'Pitch', 'Yaw'], mark_nan=True)
    p.change_dataset(actuator_controls_0.thrust_sp_topic)
    if actuator_controls_0.thrust_z_neg is not None:
        p.add_graph([lambda d: ('thrust', actuator_controls_0.thrust_z_neg)], colors8[3:4],
                    ['Thrust (up)'], mark_nan=True)
    if actuator_controls_0.thrust_x is not None:
        p.add_graph([lambda d: ('thrust', actuator_controls_0.thrust_x)], colors8[4:5],
                    ['Thrust (forward)'], mark_nan=True)
    add(p)

    # Actuator controls FFT
    p = _Plot(ctx, actuator_controls_0.torque_sp_topic, 'Actuator Controls FFT', 'Amplitude',
              y_range=(0, 0.01), kind='fft', x_axis_label='Hz')
    p.add_fft(actuator_controls_0.torque_axes_field_names, colors3, ['Roll', 'Pitch', 'Yaw'])
    if not p.had_error:
        for prm in ('MC_DTERM_CUTOFF', 'IMU_DGYRO_CUTOFF', 'IMU_GYRO_CUTOFF'):
            if prm in params:
                p.mark_frequency(params[prm], prm)
    add(p)

    # Angular velocity FFT
    p = _Plot(ctx, 'vehicle_angular_velocity', 'Angular Velocity FFT', 'Amplitude',
              y_range=(0, 0.01), kind='fft', x_axis_label='Hz')
    p.add_fft(['xyz[0]', 'xyz[1]', 'xyz[2]'], colors3, ['Rollspeed', 'Pitchspeed', 'Yawspeed'])
    if not p.had_error:
        if 'IMU_GYRO_CUTOFF' in params:
            p.mark_frequency(params['IMU_GYRO_CUTOFF'], 'IMU_GYRO_CUTOFF')
        if params.get('IMU_GYRO_NF_FREQ', 0) > 0:
            p.mark_frequency(params['IMU_GYRO_NF_FREQ'], 'IMU_GYRO_NF_FREQ')
    add(p)

    # Angular acceleration FFT
    p = _Plot(ctx, 'vehicle_angular_acceleration', 'Angular Acceleration FFT', 'Amplitude',
              kind='fft', x_axis_label='Hz')
    p.add_fft(['xyz[0]', 'xyz[1]', 'xyz[2]'], colors3, ['Roll accel', 'Pitch accel', 'Yaw accel'])
    if not p.had_error:
        if 'IMU_DGYRO_CUTOFF' in params:
            p.mark_frequency(params['IMU_DGYRO_CUTOFF'], 'IMU_DGYRO_CUTOFF')
        if params.get('IMU_GYRO_NF_FREQ', 0) > 0:
            p.mark_frequency(params['IMU_GYRO_NF_FREQ'], 'IMU_GYRO_NF_FREQ')
    add(p)

    # Actuator controls 1 (VTOL / fixed-wing)
    p = _Plot(ctx, actuator_controls_1.torque_sp_topic,
              'Actuator Controls 1 (VTOL in Fixed-Wing mode)', y_start=0,
              topic_instance=actuator_controls_1.topic_instance)
    p.add_graph(actuator_controls_1.torque_axes_field_names, colors8[0:3],
                ['Roll', 'Pitch', 'Yaw'], mark_nan=True)
    p.change_dataset(actuator_controls_1.thrust_sp_topic, actuator_controls_1.topic_instance)
    if actuator_controls_1.thrust_x is not None:
        p.add_graph([lambda d: ('thrust', actuator_controls_1.thrust_x)], colors8[3:4],
                    ['Thrust (forward)'], mark_nan=True)
    add(p)

    if dynamic_control_alloc:
        for topic_name, plot_name in [('actuator_motors', 'Motor'), ('actuator_servos', 'Servo')]:
            p = _Plot(ctx, topic_name, plot_name + ' Outputs', y_range=(-1, 1))
            n_out = 12
            if p.dataset is not None:
                for i in range(n_out):
                    out = p.dataset.get(f'control[{i}]')
                    if out is None or np.isnan(out).all():
                        n_out = i
                        break
                if n_out > 0:
                    p.add_graph([f'control[{i}]' for i in range(n_out)],
                                [colors8[i % 8] for i in range(n_out)],
                                [f'{plot_name} {i + 1}' for i in range(n_out)])
                    add(p)
    else:
        for inst, plot_name in [(0, 'Actuator Outputs (Main)'), (1, 'Actuator Outputs (AUX)'),
                                (2, 'Actuator Outputs (EXTRA)')]:
            p = _Plot(ctx, 'actuator_outputs', plot_name, y_start=0, topic_instance=inst)
            n_out = 16
            all_constant = True
            if p.dataset is not None:
                n_out = int(min(np.amax(p.dataset['noutputs']), n_out))
                for i in range(n_out):
                    o = p.dataset[f'output[{i}]']
                    if not np.all(o == o[0]):
                        all_constant = False
            if not all_constant:
                p.add_graph([f'output[{i}]' for i in range(n_out)],
                            [colors8[i % 8] for i in range(n_out)],
                            [f'Output {i}' for i in range(n_out)], mark_nan=True)
                add(p)

    # ESC RPM
    p = _Plot(ctx, 'esc_status', 'Motor RPM', 'RPM')
    if not p.had_error and 'esc_count' in p.dataset:
        for i in range(int(p.dataset['esc_count'][0])):
            f = f'esc[{i}].esc_rpm'
            if f in p.dataset and np.amax(p.dataset[f]) > 0.001:
                p.add_graph([f], [colors8[i % 8]], [f'ESC {i} RPM'])
        add(p)

    # Raw acceleration
    p = _Plot(ctx, 'sensor_combined', 'Raw Acceleration', '[m/s^2]')
    p.add_graph(['accelerometer_m_s2[0]', 'accelerometer_m_s2[1]', 'accelerometer_m_s2[2]'],
                colors3, ['X', 'Y', 'Z'])
    add(p)

    # Vibration metrics
    try:
        ver_sw_release = int(ulog.msg_info_dict.get('ver_sw_release', 0))
    except (TypeError, ValueError):
        ver_sw_release = 0
    vibration_old_units = 0 < ver_sw_release < 0x010d0000
    unit = '[m/s]' if vibration_old_units else '[m/s^2]'
    p = _Plot(ctx, 'vehicle_imu_status', 'Vibration Metrics', y_start=0, topic_instance=0,
              modes=False)
    p.add_graph(['accel_vibration_metric'], colors8[0:1], ['Accel 0 Vibration Level ' + unit])
    for i in (1, 2, 3):
        p.change_dataset('vehicle_imu_status', i)
        p.add_graph(['accel_vibration_metric'], colors8[i:i + 1],
                    [f'Accel {i} Vibration Level ' + unit])
    if not vibration_old_units:
        p.add_horizontal_background_boxes(['green', 'orange', 'red'], [4.905, 9.81])
    add(p)

    # Spectrograms
    p = _Plot(ctx, 'sensor_combined', 'Acceleration Power Spectral Density', '[Hz]',
              kind='spectrogram')
    p.add_spectrogram(['accelerometer_m_s2[0]', 'accelerometer_m_s2[1]', 'accelerometer_m_s2[2]'],
                      ['X', 'Y', 'Z'])
    add(p)
    p = _Plot(ctx, 'vehicle_angular_velocity', 'Angular velocity Power Spectral Density', '[Hz]',
              kind='spectrogram')
    p.add_spectrogram(['xyz[0]', 'xyz[1]', 'xyz[2]'], ['rollspeed', 'pitchspeed', 'yawspeed'])
    add(p)
    p = _Plot(ctx, 'vehicle_angular_acceleration', 'Angular acceleration Power Spectral Density',
              '[Hz]', kind='spectrogram')
    p.add_spectrogram(['xyz[0]', 'xyz[1]', 'xyz[2]'], ['roll accel', 'pitch accel', 'yaw accel'])
    add(p)

    # Raw angular speed
    p = _Plot(ctx, 'sensor_combined', 'Raw Angular Speed (Gyroscope)', '[deg/s]')
    p.add_graph([lambda d, i=i: (f'gyro_rad[{i}]', np.rad2deg(d[f'gyro_rad[{i}]'])) for i in range(3)],
                colors3, ['X', 'Y', 'Z'])
    add(p)

    # FIFO accel / gyro
    for instance in range(3):
        if ctx.add_virtual_fifo_topic_data('sensor_accel_fifo', instance):
            p = _Plot(ctx, 'sensor_accel_fifo_virtual', f'Raw Acceleration (FIFO, IMU{instance})',
                      '[m/s^2]', topic_instance=instance, modes=False)
            p.add_graph(['x', 'y', 'z'], colors3, ['X', 'Y', 'Z'])
            add(p)
            p = _Plot(ctx, 'sensor_accel_fifo_virtual',
                      f'Acceleration Power Spectral Density(FIFO, IMU{instance})', '[Hz]',
                      topic_instance=instance, kind='spectrogram')
            p.add_spectrogram(['x', 'y', 'z'], ['X', 'Y', 'Z'])
            add(p)
            p = _Plot(ctx, 'sensor_accel_fifo',
                      f'Sampling Regularity of Sensor Data (FIFO, IMU{instance})', '[us]',
                      y_range=(0, 25e3), topic_instance=instance, modes=False)
            if p.dataset is not None:
                diff = np.diff(p.dataset['timestamp'].astype(np.int64))
                p.add_dropouts(ulog.dropouts, float(np.amin(diff)) if len(diff) else 0.0)
                p.add_graph([lambda d: ('timediff', np.append(diff, 0))], [colors3[2]],
                            ['delta t (between 2 logged samples)'])
            add(p)
    for instance in range(3):
        if ctx.add_virtual_fifo_topic_data('sensor_gyro_fifo', instance):
            p = _Plot(ctx, 'sensor_gyro_fifo_virtual', f'Raw Gyro (FIFO, IMU{instance})',
                      '[deg/s]', topic_instance=instance, modes=False)
            p.add_graph(['x', 'y', 'z'], colors3, ['X', 'Y', 'Z'])
            p.add_graph([lambda d, a=a: (a, np.rad2deg(d[a])) for a in 'xyz'], colors3,
                        ['X', 'Y', 'Z'])
            add(p)
            p = _Plot(ctx, 'sensor_gyro_fifo_virtual',
                      f'Gyro Power Spectral Density (FIFO, IMU{instance})', '[Hz]',
                      topic_instance=instance, kind='spectrogram')
            p.add_spectrogram(['x', 'y', 'z'], ['X', 'Y', 'Z'])
            add(p)

    # Magnetic field strength
    p = _Plot(ctx, magnetometer_ga_topic, 'Raw Magnetic Field Strength', '[gauss]')
    p.add_graph(['magnetometer_ga[0]', 'magnetometer_ga[1]', 'magnetometer_ga[2]'], colors3,
                ['X', 'Y', 'Z'])
    add(p)

    # Distance sensor
    p = _Plot(ctx, 'distance_sensor', 'Distance Sensor', '[m]', y_start=0)
    p.add_graph(['current_distance', 'variance'], colors3[0:2], ['Distance', 'Variance'])
    p.change_dataset('vehicle_local_position')
    p.add_graph(['dist_bottom', 'dist_bottom_valid'], colors8[2:4],
                ['Estimated Distance Bottom [m]', 'Dist Bottom Valid'])
    add(p)

    # GPS uncertainty
    p = _Plot(ctx, gnss.name, 'GPS Uncertainty', y_range=(0, 40))
    p.add_graph([gnss.field(f) for f in ['eph', 'epv', 'hdop', 'vdop', 'speed_accuracy',
                                         'satellites_used', 'fix_type']], colors8,
                ['Horizontal position accuracy [m]', 'Vertical position accuracy [m]',
                 'Horizontal dilution of precision [m]', 'Vertical dilution of precision [m]',
                 'Speed accuracy [m/s]', 'Num Satellites used', 'GPS Fix'])
    add(p)

    # GPS noise & jamming
    p = _Plot(ctx, gnss.name, 'GPS Noise & Jamming', y_start=0)
    p.add_graph([gnss.field('noise'), gnss.field('jamming_indicator')], colors3[0:2],
                ['Noise per ms', 'Jamming Indicator'])
    add(p)

    # Thrust and magnetic field
    p = _Plot(ctx, magnetometer_ga_topic, 'Thrust and Magnetic Field', y_start=0)
    p.add_graph([lambda d: ('len_mag', np.sqrt(d['magnetometer_ga[0]'] ** 2 +
                                               d['magnetometer_ga[1]'] ** 2 +
                                               d['magnetometer_ga[2]'] ** 2))],
                colors3[0:1], ['Norm of Magnetic Field'])
    p.change_dataset(actuator_controls_0.thrust_sp_topic)
    if actuator_controls_0.thrust is not None:
        p.add_graph([lambda d: ('thrust', actuator_controls_0.thrust)], colors3[1:2], ['Thrust'])
    if is_vtol and not dynamic_control_alloc:
        p.change_dataset(actuator_controls_1.thrust_sp_topic)
        if actuator_controls_1.thrust_x is not None:
            p.add_graph([lambda d: ('thrust', actuator_controls_1.thrust_x)], colors3[2:3],
                        ['Thrust (Fixed-wing'])
    add(p)

    # Power
    p = _Plot(ctx, 'battery_status', 'Power', y_start=0)
    p.add_graph(['voltage_v', 'current_a',
                 lambda d: ('discharged_mah', d['discharged_mah'] / 100),
                 lambda d: ('remaining', d['remaining'] * 10)],
                colors8[0:4], ['Battery Voltage [V]', 'Battery Current [A]',
                               'Discharged Amount [mAh / 100]',
                               'Battery remaining [0=empty, 10=full]'])
    if p.dataset is not None and 'internal_resistance_estimate' in p.dataset:
        p.add_graph(['ocv_estimate',
                     lambda d: ('internal_resistance_estimate', d['internal_resistance_estimate'] * 1000)],
                    colors8[4:6], ['OCV Estimate [V]', 'Internal Resistance Estimate [mOhm]'])
    p.change_dataset('system_power')
    if p.dataset is not None:
        if 'voltage5v_v' in p.dataset and np.amax(p.dataset['voltage5v_v']) > 0.0001:
            p.add_graph(['voltage5v_v'], colors8[7:8], ['5 V'])
        if 'sensors3v3[0]' in p.dataset and np.amax(p.dataset['sensors3v3[0]']) > 0.0001:
            p.add_graph(['sensors3v3[0]'], colors8[6:7], ['3.3 V'])
    add(p)

    # Temperature
    p = _Plot(ctx, 'sensor_baro', 'Temperature', '[C]')
    p.add_graph(['temperature'], colors8[0:1], ['Baro temperature'])
    p.change_dataset('sensor_accel')
    p.add_graph(['temperature'], colors8[2:3], ['Accel temperature'])
    p.change_dataset('airspeed')
    p.add_graph(['air_temperature_celsius'], colors8[4:5], ['Airspeed temperature'])
    p.change_dataset('battery_status')
    p.add_graph(['temperature'], colors8[6:7], ['Battery temperature'])
    p.change_dataset('esc_status')
    if p.dataset is not None and 'esc_count' in p.dataset:
        for i in range(int(p.dataset['esc_count'][0])):
            f = f'esc[{i}].esc_temperature'
            if f in p.dataset and np.amax(p.dataset[f]) > 0.001:
                p.add_graph([f], [colors8[i % 8]], [f'ESC {i} temperature'])
    add(p)

    # Estimator flags
    es = ctx.ds('estimator_status')
    if es is not None:
        try:
            p = _Plot(ctx, 'estimator_status', 'Estimator Flags', y_start=0, modes=False)
            input_data = [('Health Flags (vel, pos, hgt)', es['health_flags']),
                          ('Timeout Flags (vel, pos, hgt)', es['timeout_flags'])]
            if 'innovation_check_flags' in es:
                icf = es['innovation_check_flags']
                input_data += [
                    ('Velocity Check Bit', icf & 0x1),
                    ('Horizontal Position Check Bit', (icf >> 1) & 1),
                    ('Vertical Position Check Bit', (icf >> 2) & 1),
                    ('Mag X, Y, Z Check Bits', (icf >> 3) & 0x7),
                    ('Yaw Check Bit', (icf >> 6) & 1),
                    ('Airspeed Check Bit', (icf >> 7) & 1),
                    ('Synthetic Sideslip Check Bit', (icf >> 8) & 1),
                    ('Height to Ground Check Bit', (icf >> 9) & 1),
                    ('Optical Flow X, Y Check Bits', (icf >> 10) & 0x3)]
            plot_data, plot_labels = [], []
            for label, data in input_data:
                if np.amax(data) > 0.1:
                    plot_data.append(lambda d, data=data: ('flags', data))
                    plot_labels.append(label)
                    if len(plot_data) >= 8:
                        break
            if not plot_data:
                plot_data = [lambda d: ('flags', input_data[0][1])]
                plot_labels = [input_data[0][0]]
            p.add_graph(plot_data, colors8[0:len(plot_data)], plot_labels)
            add(p)
        except _ERRORS:
            pass

    # Failsafe flags
    p = _Plot(ctx, 'vehicle_status', 'Failsafe Flags', y_start=0)
    p.add_graph(['failsafe', 'failsafe_and_user_took_over'], [colors8[0], colors8[1]],
                ['In Failsafe', 'User Took Over'])
    num_graphs = 2
    skip_if_always_set = ['auto_mission_missing', 'offboard_control_signal_lost']
    p.change_dataset('failsafe_flags')
    if p.dataset is not None:
        for fld, data in p.dataset.items():
            if fld == 'timestamp' or fld.startswith('mode_req_') or not len(data):
                continue
            if np.amax(data) >= 1:
                if fld in skip_if_always_set and np.amin(data) >= 1:
                    continue
                p.add_graph([fld], [colors8[num_graphs % 8]], [fld.replace('_', ' ')])
                num_graphs += 1
        add(p)

    # CPU & RAM
    p = _Plot(ctx, 'cpuload', 'CPU & RAM', y_range=(0, 1))
    p.add_graph(['ram_usage', 'load'], [colors3[1], colors3[2]], ['RAM Usage', 'CPU Load'])
    p.add_span('load', line_color=colors3[2])
    p.add_span('ram_usage', line_color=colors3[1])
    add(p)

    # Sampling regularity
    sc = ctx.ds('sensor_combined')
    if sc is not None and len(sc['timestamp']) > 1:
        p = _Plot(ctx, 'sensor_combined', 'Sampling Regularity of Sensor Data', '[us]',
                  y_range=(0, 25e3))
        diff = np.diff(sc['timestamp'].astype(np.int64))
        p.add_dropouts(ulog.dropouts, float(np.amin(diff)))
        p.add_graph([lambda d: ('timediff', np.append(diff, 0))], [colors3[2]],
                    ['delta t (between 2 logged samples)'])
        p.change_dataset('estimator_status')
        p.add_graph([lambda d: ('time_slip', d['time_slip'] * 1e6)], [colors3[1]],
                    ['Estimator time slip (cumulative)'])
        add(p)

    # make ids unique (titles are unique in practice; be safe)
    seen: dict[str, int] = {}
    for g in plots:
        if g.id in seen:
            seen[g.id] += 1
            g.id = f'{g.id}_{seen[g.id]}'
        else:
            seen[g.id] = 0
    return plots


def build_track(fl: FlightLog) -> TrackDef | None:
    """Flight Review's 'Position' 2D plot (East vs North in the local frame)."""
    ctx = _Ctx(fl)
    lp = ctx.ds('vehicle_local_position')
    if lp is None or 'x' not in lp:
        return None
    x, y = lp['x'].astype(np.float64), lp['y'].astype(np.float64)
    ok = ~(np.isnan(x) | np.isnan(y))
    if np.count_nonzero(x[ok]) == 0 and np.count_nonzero(y[ok]) == 0:
        return None
    t = ctx.sec(lp['timestamp'])
    series = [Series('Estimated', colors2[0], y[ok], x[ok])]
    sp = ctx.ds('vehicle_local_position_setpoint')
    if sp is not None and 'x' in sp:
        sx, sy = sp['x'].astype(np.float64), sp['y'].astype(np.float64)
        sok = ~(np.isnan(sx) | np.isnan(sy))
        series.append(Series('Setpoint', colors2[1], sy[sok], sx[sok]))
    gt = ctx.ds('vehicle_local_position_groundtruth')
    if gt is not None:
        series.append(Series('Groundtruth', color_gray, gt['y'].astype(np.float64),
                             gt['x'].astype(np.float64)))
    return TrackDef(series, t[ok], y[ok], x[ok])
