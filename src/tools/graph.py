import argparse
import json
import math
import os
import time


FRONTEND_PHASES = {"fir", "frontend"}
BACKEND_PHASES = {"cb", "backend"}


def read_json_retry(path, attempts=20, delay=0.05):
    last_exc = None
    for _ in range(attempts):
        try:
            with open(path) as f:
                return json.load(f)
        except json.JSONDecodeError as exc:
            last_exc = exc
            time.sleep(delay)
    raise last_exc


def n_common_points(stats, faults):
    processed = (
            stats["totals"].get("passed", 0) +
            stats["totals"].get("failed", 0)
    )

    fir_ids = error_phase_ids(faults, FRONTEND_PHASES)

    points = []
    fir_seen = 0
    idx = 0

    for n in range(processed + 1):
        while idx < len(fir_ids) and fir_ids[idx] <= n:
            fir_seen += 1
            idx += 1
        points.append((n, n - fir_seen))

    return points


def error_phase_ids(faults, phases):
    return sorted(
        int(pid)
        for pid, fault in faults.items()
        if fault.get("error_phase") in phases
    )


def points_by_id(points, ids):
    return [
        points[pid]
        for pid in ids
        if 0 <= pid < len(points)
    ]


def nice_ticks(max_value, target=10):
    if max_value <= 0:
        return [0]

    raw_step = max_value / target
    magnitude = 1
    while magnitude * 10 <= raw_step:
        magnitude *= 10

    for candidate in (1, 2, 5, 10):
        step = candidate * magnitude
        if step >= raw_step:
            break

    ticks = list(range(0, int(max_value) + 1, int(step)))
    if ticks[-1] != max_value:
        ticks.append(max_value)
    return ticks


def nice_time_ticks(max_value, target=10):
    if max_value <= 0:
        return [0.0]

    raw_step = max_value / target
    magnitude = 10 ** math.floor(math.log10(raw_step))

    for candidate in (1, 2, 5, 10):
        step = candidate * magnitude
        if step >= raw_step:
            break

    ticks = []
    tick = 0.0
    while tick <= max_value:
        ticks.append(tick)
        tick += step
    if not math.isclose(ticks[-1], max_value):
        ticks.append(max_value)
    return ticks


def format_seconds(value):
    if math.isclose(value, round(value)):
        return str(int(round(value)))
    if value >= 10:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{value:.2f}".rstrip("0").rstrip(".")


def time_metric_value(metrics, field, pid):
    if not hasattr(metrics, "get"):
        raise SystemExit(
            f"Invalid metrics for program {pid}: expected an object")
    value = metrics.get(field, 0.0)
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(
            f"Invalid {field} for program {pid}: {value!r}") from exc


def sorted_time_metrics(time_metrics):
    try:
        return sorted(
            (int(pid), metrics)
            for pid, metrics in time_metrics.items()
        )
    except ValueError as exc:
        raise SystemExit(
            "time_metrics.json contains a non-numeric program id") from exc


def cumulative_time_points(time_metrics):
    compilation_points = [(0, 0.0)]
    total_points = [(0, 0.0)]
    compilation_total = 0.0
    generation_total = 0.0

    for pid, metrics in sorted_time_metrics(time_metrics):
        generation_cpu = time_metric_value(metrics, "generation_cpu", pid)
        compilation = time_metric_value(
            metrics, "compilation_with_ir_dumps", pid)
        phase_profiling = time_metric_value(
            metrics, "phase_profiling", pid)

        compilation_total += compilation + phase_profiling
        generation_total += generation_cpu
        compilation_points.append((pid, compilation_total))
        total_points.append((pid, compilation_total + generation_total))

    return compilation_points, total_points


def write_svg(points, out_file, frontend_ids=None, backend_ids=None,
              width=1200, height=700):
    margin = 70
    max_x = max(x for x, _ in points) or 1
    max_y = max(y for _, y in points) or 1

    def sx(x):
        return margin + x * (width - 2 * margin) / max_x

    def sy(y):
        return height - margin - y * (height - 2 * margin) / max_y

    x_ticks = nice_ticks(max_x)
    y_ticks = nice_ticks(max_y)
    polyline = " ".join(f"{sx(x):.2f},{sy(y):.2f}" for x, y in points)
    frontend_points = points_by_id(points, frontend_ids or [])
    backend_points = points_by_id(points, backend_ids or [])

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="white"/>',
    ]

    for x in x_ticks:
        px = sx(x)
        parts.append(
            f'<line x1="{px:.2f}" y1="{margin}" x2="{px:.2f}" y2="{height - margin}" stroke="#e5e5e5"/>'
        )
        parts.append(
            f'<text x="{px:.2f}" y="{height - margin + 22}" text-anchor="middle" font-size="12">{x}</text>'
        )

    for y in y_ticks:
        py = sy(y)
        parts.append(
            f'<line x1="{margin}" y1="{py:.2f}" x2="{width - margin}" y2="{py:.2f}" stroke="#e5e5e5"/>'
        )
        parts.append(
            f'<text x="{margin - 12}" y="{py + 4:.2f}" text-anchor="end" font-size="12">{y}</text>'
        )

    parts.extend([
        f'<line x1="{margin}" y1="{height - margin}" x2="{width - margin}" y2="{height - margin}" stroke="black"/>',
        f'<line x1="{margin}" y1="{margin}" x2="{margin}" y2="{height - margin}" stroke="black"/>',
        f'<polyline points="{polyline}" fill="none" stroke="#1f77b4" stroke-width="2"/>',
    ])

    for x, y in backend_points:
        parts.append(
            f'<circle cx="{sx(x):.2f}" cy="{sy(y):.2f}" r="3" fill="#2ca02c"/>'
        )

    for x, y in frontend_points:
        parts.append(
            f'<circle cx="{sx(x):.2f}" cy="{sy(y):.2f}" r="3" fill="#d62728"/>'
        )

    parts.extend([
        f'<text x="{width / 2}" y="{height - 20}" text-anchor="middle" font-size="14">n processed</text>',
        f'<text x="18" y="{height / 2}" transform="rotate(-90 18,{height / 2})" text-anchor="middle" font-size="14">n_common</text>',
        '</svg>',
    ])

    with open(out_file, "w") as f:
        f.write("\n".join(parts))


def write_time_svg(compilation_points, total_points, out_file,
                   width=1200, height=700):
    margin = 80
    max_x = max(x for x, _ in total_points) or 1
    max_y = max(y for _, y in total_points) or 1

    def sx(x):
        return margin + x * (width - 2 * margin) / max_x

    def sy(y):
        return height - margin - y * (height - 2 * margin) / max_y

    def polyline(points):
        return " ".join(f"{sx(x):.2f},{sy(y):.2f}" for x, y in points)

    x_ticks = nice_ticks(max_x)
    y_ticks = nice_time_ticks(max_y)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="white"/>',
    ]

    for x in x_ticks:
        px = sx(x)
        parts.append(
            f'<line x1="{px:.2f}" y1="{margin}" x2="{px:.2f}" y2="{height - margin}" stroke="#e5e5e5"/>'
        )
        parts.append(
            f'<text x="{px:.2f}" y="{height - margin + 22}" text-anchor="middle" font-size="12">{x}</text>'
        )

    for y in y_ticks:
        py = sy(y)
        label = format_seconds(y)
        parts.append(
            f'<line x1="{margin}" y1="{py:.2f}" x2="{width - margin}" y2="{py:.2f}" stroke="#e5e5e5"/>'
        )
        parts.append(
            f'<text x="{margin - 12}" y="{py + 4:.2f}" text-anchor="end" font-size="12">{label}</text>'
        )

    legend_x = width - margin - 320
    legend_y = margin + 10

    parts.extend([
        f'<line x1="{margin}" y1="{height - margin}" x2="{width - margin}" y2="{height - margin}" stroke="black"/>',
        f'<line x1="{margin}" y1="{margin}" x2="{margin}" y2="{height - margin}" stroke="black"/>',
        f'<polyline points="{polyline(compilation_points)}" fill="none" stroke="orange" stroke-width="2"/>',
        f'<polyline points="{polyline(total_points)}" fill="none" stroke="black" stroke-width="2"/>',
        f'<line x1="{legend_x}" y1="{legend_y}" x2="{legend_x + 34}" y2="{legend_y}" stroke="orange" stroke-width="2"/>',
        f'<text x="{legend_x + 44}" y="{legend_y + 4}" font-size="13">compilation + metrics</text>',
        f'<line x1="{legend_x}" y1="{legend_y + 24}" x2="{legend_x + 34}" y2="{legend_y + 24}" stroke="black" stroke-width="2"/>',
        f'<text x="{legend_x + 44}" y="{legend_y + 28}" font-size="13">generation + compilation + metrics</text>',
        f'<text x="{width / 2}" y="{height - 20}" text-anchor="middle" font-size="14">program n</text>',
        f'<text x="18" y="{height / 2}" transform="rotate(-90 18,{height / 2})" text-anchor="middle" font-size="14">cumulative seconds</text>',
        '</svg>',
    ])

    with open(out_file, "w") as f:
        f.write("\n".join(parts))


def graph_n_common(run_dir):
    stats = read_json_retry(os.path.join(run_dir, "stats.json"))
    faults = read_json_retry(os.path.join(run_dir, "faults.json"))

    points = n_common_points(stats, faults)
    frontend_ids = error_phase_ids(faults, FRONTEND_PHASES)
    backend_ids = error_phase_ids(faults, BACKEND_PHASES)
    out_file = os.path.join(run_dir, "graph_n_common.svg")
    write_svg(points, out_file, frontend_ids, backend_ids)
    return out_file


def graph_time(run_dir):
    metrics_file = os.path.join(run_dir, "time_metrics.json")
    if not os.path.exists(metrics_file):
        raise SystemExit(
            "Missing time_metrics.json; run Hephaestus with --time-metrics")

    time_metrics = read_json_retry(metrics_file)
    if not time_metrics:
        raise SystemExit(
            "Empty time_metrics.json; run Hephaestus with --time-metrics")

    compilation_points, total_points = cumulative_time_points(time_metrics)
    out_file = os.path.join(run_dir, "graph_time.svg")
    write_time_svg(compilation_points, total_points, out_file)
    return out_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("graph", choices=["n_common", "time"])
    parser.add_argument("run_dir")
    args = parser.parse_args()

    run_dir = os.path.abspath(args.run_dir)

    if args.graph == "n_common":
        out_file = graph_n_common(run_dir)
        print(out_file)
    elif args.graph == "time":
        out_file = graph_time(run_dir)
        print(out_file)


if __name__ == "__main__":
    main()
