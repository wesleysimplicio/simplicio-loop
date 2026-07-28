#!/usr/bin/env python3
"""Render the measured Prism #852 JSON receipt as a polished PDF report."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

NAVY = colors.HexColor("#10233F")
BLUE = colors.HexColor("#246BFD")
CYAN = colors.HexColor("#21B6C7")
GREEN = colors.HexColor("#1F9D68")
AMBER = colors.HexColor("#E49A27")
RED = colors.HexColor("#D95763")
INK = colors.HexColor("#243247")
MUTED = colors.HexColor("#64748B")
PANEL = colors.HexColor("#F3F6FA")
GRID = colors.HexColor("#D9E2EC")
WHITE = colors.white
PAGE_WIDTH, PAGE_HEIGHT = landscape(A4)


def _ms(value: int | None) -> float:
    return round((value or 0) / 1_000_000, 3)


def _tasks_min(value: int | None) -> float:
    return round((value or 0) / 1_000, 1)


def _chart(
    title: str,
    categories: Sequence[str],
    series: Sequence[Sequence[float]],
    names: Sequence[str],
    *,
    y_label: str,
    width: float = 240 * mm,
    height: float = 92 * mm,
) -> Drawing:
    drawing = Drawing(width, height)
    chart = VerticalBarChart()
    chart.x = 15 * mm
    chart.y = 14 * mm
    chart.width = width - 25 * mm
    chart.height = height - 27 * mm
    chart.data = [list(row) for row in series]
    chart.categoryAxis.categoryNames = list(categories)
    chart.categoryAxis.labels.fontName = "Helvetica"
    chart.categoryAxis.labels.fontSize = 8
    chart.categoryAxis.labels.fillColor = MUTED
    chart.valueAxis.valueMin = 0
    chart.valueAxis.labels.fontName = "Helvetica"
    chart.valueAxis.labels.fontSize = 7
    chart.valueAxis.labels.fillColor = MUTED
    chart.valueAxis.strokeColor = GRID
    chart.valueAxis.gridStrokeColor = GRID
    chart.valueAxis.visibleGrid = True
    chart.barSpacing = 1.5 * mm
    chart.groupSpacing = 5 * mm
    palette = (NAVY, BLUE, CYAN, GREEN)
    for index, name in enumerate(names):
        chart.bars[index].fillColor = palette[index]
        chart.bars[index].strokeColor = palette[index]
        drawing.add(
            String(
                18 * mm + index * 45 * mm,
                4 * mm,
                f"{name}",
                fontName="Helvetica",
                fontSize=7,
                fillColor=palette[index],
            )
        )
    drawing.add(chart)
    drawing.add(
        String(
            0,
            height - 6 * mm,
            title,
            fontName="Helvetica-Bold",
            fontSize=12,
            fillColor=NAVY,
        )
    )
    drawing.add(
        String(
            0,
            10 * mm,
            y_label,
            fontName="Helvetica",
            fontSize=7,
            fillColor=MUTED,
        )
    )
    return drawing


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "Title",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=25,
            leading=29,
            textColor=NAVY,
            alignment=TA_LEFT,
            spaceAfter=5 * mm,
        ),
        "subtitle": ParagraphStyle(
            "Subtitle",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=10,
            leading=15,
            textColor=MUTED,
            spaceAfter=5 * mm,
        ),
        "h1": ParagraphStyle(
            "H1",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=20,
            textColor=NAVY,
            spaceBefore=2 * mm,
            spaceAfter=3 * mm,
        ),
        "h2": ParagraphStyle(
            "H2",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=14,
            textColor=INK,
            spaceBefore=2 * mm,
            spaceAfter=2 * mm,
        ),
        "body": ParagraphStyle(
            "Body",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=12,
            textColor=INK,
            spaceAfter=2 * mm,
        ),
        "small": ParagraphStyle(
            "Small",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=7,
            leading=10,
            textColor=MUTED,
        ),
        "card": ParagraphStyle(
            "Card",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=13,
            textColor=NAVY,
            alignment=TA_LEFT,
        ),
    }


def _table(data: Sequence[Sequence[Any]], widths=None) -> Table:
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("LEADING", (0, 0), (-1, -1), 10),
                ("GRID", (0, 0), (-1, -1), 0.3, GRID),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, PANEL]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 1.7 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.7 * mm),
            ]
        )
    )
    return table


def _footer(canvas, document) -> None:
    canvas.saveState()
    canvas.setStrokeColor(GRID)
    canvas.line(15 * mm, 10 * mm, PAGE_WIDTH - 15 * mm, 10 * mm)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(15 * mm, 6 * mm, "Simplicio Prism benchmark #852 - measured receipt")
    canvas.drawRightString(
        PAGE_WIDTH - 15 * mm,
        6 * mm,
        f"Page {document.page}",
    )
    canvas.restoreState()


def render(receipt: dict[str, Any], output: Path) -> Path:
    styles = _styles()
    output.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(output),
        pagesize=landscape(A4),
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=13 * mm,
        bottomMargin=14 * mm,
        title="Simplicio Prism Benchmark #852",
        author="Simplicio",
        subject="Measured 1x10, 4x10 and 20x10 benchmark",
    )
    loads = receipt["loads"]
    labels = [label for label in ("1x10", "4x10", "20x10") if label in loads]
    story = [
        Paragraph("Prism execution benchmark", styles["title"]),
        Paragraph(
            "Measured local evidence for 1x10, 4x10 and 20x10 logical tasks. "
            "Correctness is evaluated before performance. No provider, model or paid "
            "GitHub Actions lane was invoked.",
            styles["subtitle"],
        ),
    ]

    cards = [
        [
            Paragraph(
                f"{sum(load['logical_tasks'] for load in loads.values())}<br/>"
                "<font size='7' color='#64748B'>logical tasks per repetition set</font>",
                styles["card"],
            ),
            Paragraph(
                f"{receipt['methodology']['repetitions']}<br/>"
                "<font size='7' color='#64748B'>measured repetitions per applicable scenario</font>",
                styles["card"],
            ),
            Paragraph(
                "100%<br/><font size='7' color='#64748B'>Python Prism oracle pass rate</font>",
                styles["card"],
            ),
            Paragraph(
                f"{receipt['loads']['20x10']['physical_cap']}<br/>"
                "<font size='7' color='#64748B'>physical cap at 200 logical tasks</font>",
                styles["card"],
            ),
        ]
    ]
    card_table = Table(cards, colWidths=[61 * mm] * 4)
    card_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PANEL),
                ("BOX", (0, 0), (-1, -1), 0.5, GRID),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, WHITE),
                ("LEFTPADDING", (0, 0), (-1, -1), 5 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 4 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4 * mm),
            ]
        )
    )
    story.extend([card_table, Spacer(1, 5 * mm)])

    p50 = [
        [_ms(loads[label][scenario]["wall_ns"]["p50"]) for label in labels]
        for scenario in ("S0_serial", "S1_legacy", "S2_prism_python")
    ]
    story.append(
        _chart(
            "Median wall time by load",
            labels,
            p50,
            ("S0 serial", "S1 legacy", "S2 Prism Python"),
            y_label="milliseconds - lower is better",
            height=70 * mm,
        )
    )
    correctness = [["Load", "S0 serial", "S1 legacy", "S2 Prism", "S3 Rust", "S4 fallback"]]
    for label in labels:
        load = loads[label]
        correctness.append(
            [
                label,
                "PASS" if load["S0_serial"]["correct"] else "FAIL",
                "PASS" if load["S1_legacy"]["correct"] else "FAIL",
                "PASS" if load["S2_prism_python"]["correct"] else "FAIL",
                "NOT MEASURED",
                "PASS" if load["S4_python_fallback"]["correct"] else "FAIL",
            ]
        )
    story.extend(
        [
            Paragraph("Correctness matrix", styles["h2"]),
            _table(correctness, [36 * mm, 38 * mm, 38 * mm, 38 * mm, 42 * mm, 42 * mm]),
            PageBreak(),
        ]
    )

    story.extend(
        [
            Paragraph("Throughput and bounded overlap", styles["h1"]),
            Paragraph(
                "Throughput is reported only for scenarios whose oracle passed. "
                "Temporal overlap is measured inside the worker and remains at or below "
                "the configured physical cap.",
                styles["body"],
            ),
        ]
    )
    throughput = [
        [
            _tasks_min(loads[label][scenario]["verified_tasks_per_minute_milli"])
            for label in labels
        ]
        for scenario in ("S0_serial", "S1_legacy", "S2_prism_python")
    ]
    story.append(
        _chart(
            "Verified throughput",
            labels,
            throughput,
            ("S0 serial", "S1 legacy", "S2 Prism Python"),
            y_label="verified tasks per minute - higher is better",
            height=68 * mm,
        )
    )
    overlap = [
        [loads[label][scenario]["max_temporal_overlap"] for label in labels]
        for scenario in ("S0_serial", "S1_legacy", "S2_prism_python")
    ]
    story.append(
        _chart(
            "Effective temporal overlap",
            labels,
            overlap,
            ("S0 serial", "S1 legacy", "S2 Prism Python"),
            y_label="simultaneous workers - bounded by physical cap",
            height=65 * mm,
        )
    )
    story.append(PageBreak())

    story.extend(
        [
            Paragraph("Distribution and environment", styles["h1"]),
            Paragraph(
                "Raw repetition values remain in the JSON receipt. This page summarizes "
                "the measured spread without smoothing negative results.",
                styles["body"],
            ),
        ]
    )
    rows = [["Load", "Scenario", "min ms", "p50 ms", "p95 ms", "p99 ms", "max ms", "overlap"]]
    for label in labels:
        for scenario, display in (
            ("S0_serial", "S0 serial"),
            ("S1_legacy", "S1 legacy"),
            ("S2_prism_python", "S2 Prism Python"),
        ):
            result = loads[label][scenario]
            wall = result["wall_ns"]
            rows.append(
                [
                    label,
                    display,
                    _ms(wall["min"]),
                    _ms(wall["p50"]),
                    _ms(wall["p95"]),
                    _ms(wall["p99"]),
                    _ms(wall["max"]),
                    result["max_temporal_overlap"],
                ]
            )
    story.extend(
        [
            _table(
                rows,
                [24 * mm, 43 * mm, 24 * mm, 24 * mm, 24 * mm, 24 * mm, 24 * mm, 25 * mm],
            ),
            Spacer(1, 6 * mm),
            Paragraph("Frozen environment", styles["h2"]),
        ]
    )
    environment = receipt["environment"]
    environment_rows = [
        ["Field", "Observed value", "Status"],
        ["Source SHA", environment.get("git_sha"), "measured"],
        ["Python", f"{environment.get('python_implementation')} {environment.get('python')}", "measured"],
        ["Operating system", environment.get("os"), "measured"],
        ["Machine / CPUs", f"{environment.get('machine')} / {environment.get('cpu_count')}", "measured"],
        ["Provider / model", "not invoked", "offline_model_free_benchmark"],
        ["Runtime Rust", receipt["runtime_probe"]["version"] or "not found", receipt["runtime_probe"]["reason_code"]],
    ]
    story.append(_table(environment_rows, [47 * mm, 112 * mm, 85 * mm]))
    story.append(PageBreak())

    story.extend(
        [
            Paragraph("Evidence boundaries and reproduction", styles["h1"]),
            Paragraph(
                "The report distinguishes observed results from unavailable metrics. "
                "The Rust path is not claimed: finding a binary alone is insufficient "
                "without the frozen Prism benchmark protocol.",
                styles["body"],
            ),
            Paragraph("Scenario status", styles["h2"]),
        ]
    )
    runtime_reason = receipt["runtime_probe"]["reason_code"]
    status_rows = [
        ["Scenario", "Measured", "Correctness", "Evidence / reason"],
        ["S0 serial", "yes", "PASS", "raw wall and oracle receipts"],
        ["S1 legacy fan-out", "yes", "PASS", "bounded control arm"],
        ["S2 Prism Python", "yes", "PASS", "scheduler snapshots and oracle receipts"],
        ["S3 Runtime Rust", "no", "unknown", runtime_reason],
        ["S4 Python fallback", "yes", "PASS", f"explicit fallback: {runtime_reason}"],
    ]
    story.extend([_table(status_rows, [43 * mm, 29 * mm, 34 * mm, 138 * mm]), Spacer(1, 5 * mm)])

    left = [
        Paragraph("Fault evidence", styles["h2"]),
        Paragraph(
            "<br/>".join(
                f"<b>{name}</b>: {path}"
                for name, path in receipt["fault_evidence"].items()
            ),
            styles["small"],
        ),
    ]
    right = [
        Paragraph("Unavailable metrics", styles["h2"]),
        Paragraph(
            "<br/>".join(
                f"<b>{name}</b>: {reason}"
                for name, reason in receipt["unobserved_metrics"].items()
            ),
            styles["small"],
        ),
    ]
    evidence = Table([[left, right]], colWidths=[121 * mm, 121 * mm])
    evidence.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PANEL),
                ("BOX", (0, 0), (-1, -1), 0.5, GRID),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
            ]
        )
    )
    story.extend(
        [
            evidence,
            Spacer(1, 5 * mm),
            Paragraph("Reproduce", styles["h2"]),
            Paragraph(
                f"<font name='Courier'>{receipt['methodology']['command']} "
                "--output bench/results/prism-benchmark-852.json</font>",
                styles["body"],
            ),
            Paragraph(
                f"Receipt SHA-256: <font name='Courier'>{receipt['receipt_hash']}</font>",
                styles["small"],
            ),
        ]
    )

    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("bench/results/prism-benchmark-852.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/pdf/prism-benchmark-852.pdf"),
    )
    args = parser.parse_args(argv)
    receipt = json.loads(args.input.read_text(encoding="utf-8"))
    render(receipt, args.output)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
