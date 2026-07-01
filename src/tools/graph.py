import argparse
import json
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


def graph_n_common(run_dir):
    stats = read_json_retry(os.path.join(run_dir, "stats.json"))
    faults = read_json_retry(os.path.join(run_dir, "faults.json"))

    points = n_common_points(stats, faults)
    frontend_ids = error_phase_ids(faults, FRONTEND_PHASES)
    backend_ids = error_phase_ids(faults, BACKEND_PHASES)
    out_file = os.path.join(run_dir, "graph_n_common.svg")
    write_svg(points, out_file, frontend_ids, backend_ids)
    return out_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("graph", choices=["n_common"])
    parser.add_argument("run_dir")
    args = parser.parse_args()

    run_dir = os.path.abspath(args.run_dir)

    if args.graph == "n_common":
        out_file = graph_n_common(run_dir)
        print(out_file)


if __name__ == "__main__":
    main()
