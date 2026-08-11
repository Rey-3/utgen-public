from pathlib import Path
from datetime import date

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.transforms import Bbox
from matplotlib.ticker import LogFormatterMathtext, FixedLocator


# ============================================================
# Data format:
# (group, displayed label, percentage, time taken in seconds)
# ============================================================
DATA = [
    ("CanNM", "CanNM", 86.38, 236.32),
    ("CanNM", "CaNM_if", 16.66, 0.49),

    ("DiagApp", "DiagApp", 85.36, 35.66),
    ("DiagApp", "Srv10", 36.63, 34.17),
    ("DiagApp", "Srv11", 53.84, 34.00),
    ("DiagApp", "Srv14", 65.21, 33.76),
    ("DiagApp", "Srv19", 4.00, 255.77),
    ("DiagApp", "Srv27", 12.24, 235.46),
    ("DiagApp", "Srv28", 62.06, 33.12),
    ("DiagApp", "Srv2E", 50.00, 80.82),
    ("DiagApp", "Srv2F", 66.66, 45.18),
    ("DiagApp", "Srv31", 22.98, 152.62),
    ("DiagApp", "Srv3E", 85.71, 33.29),
    ("DiagApp", "Srv85", 46.66, 33.82),
    ("DiagApp", "SrvBA", 50.00, 67.69),
    ("DiagApp", "SrvBC", 21.25, 235.28),
    ("DiagApp", "SrvBD", 7.20, 237.33),
    ("DiagApp", "SrvBE", 35.02, 245.98),

    ("DtcApp", "DtcApp", 35.13, 571.13),
    ("HwAbs", "HwAbs_CANSig", 87.08, 422.11),

    ("IoHw", "IoHw_An", 35.04, 755.62),
    ("IoHw", "IoHw_Dio", 98.52, 1.50),
    ("IoHw", "IoHw_Sig", 57.84, 512.85),

    ("NmApp", "NmApp", 96.34, 7.27),

    ("NvmApp", "NvmApp", 37.97, 3.14),
    ("NvmApp", "Nvmif", 0.00, 1030.73),

    ("OperatingVoltage", "OperatingVoltage", 62.31, 78.85),

    ("PDC", "PDC_ModeManager", 96.83, 39.00),

    ("PDM", "PDM_CANCtrl_1", 100.00, 1.93),
    ("PDM", "PDM_CANCtrl_2", 100.00, 1.45),
    ("PDM", "PDM_LocalCtrl_1", 100.00, 2.48),
    ("PDM", "PDM_LocalCtrl_2", 100.00, 1.17),
    ("PDM", "PDM_MOS_1", 90.09, 298.44),
    ("PDM", "PDM_MOS_2", 97.20, 14.82),
    ("PDM", "PDM_ProtectionCtrl_1", 97.20, 219.04),
    ("PDM", "PDM_ProtectionCtrl_2", 82.09, 213.57),

    ("PNG", "PNG_MOS", 92.53, 113.54),
    ("PNG", "PNG_Protection", 95.40, 192.46),

    ("UDS", "uds", 0.00, 39.55),

    # New AFE result set
    ("AFE", "afe_cellbalancing", 87.00, 1.13),
    ("AFE", "afe_diag_sm01", 0.00, 2.47),
    ("AFE", "afe_diag_sm02", 1.61, 20.87),
    ("AFE", "afe_diag_sm03", 0.00, 1.62),
    ("AFE", "afe_diag_sm04", 17.00, 24.00),
    ("AFE", "afe_diag_sm05", 2.00, 2.60),
    ("AFE", "afe_diag_sm06", 12.00, 1.70),
    ("AFE", "afe_diag_sm18", 3.00, 1.09),
    ("AFE", "afe_diag_sm36", 9.00, 2.23),
    ("AFE", "afe_diag_sm37", 8.00, 3.17),
    ("AFE", "afe_diag_sm38", 8.00, 2.16),
    ("AFE", "afe_diag_sm40", 4.00, 1.76),
    ("AFE", "afe_diag", 0.00, 40.25),
    ("AFE", "afe_registerBank", 71.00, 67.99),
    ("AFE", "afe_routine", 39.00, 36.84),

    # The file contains both afe module time (60.89 s) and
    # the complete AFE project total (270.79 s). The point below uses
    # the standalone afe module time because each dot represents a module.
    ("AFE", "afe", 0.00, 60.89),

    ("altFee", "altFee", 91.00, 1.29),
    ("fee", "fee", 52.00, 122.60),
    ("feeMgnt", "feeMgnt", 36.48, 237.68),

]



# ============================================================
# Data adjustments
# ============================================================
PERCENTAGE_THRESHOLD = 80.0
PERCENTAGE_MULTIPLIER = 0.92
TIME_MULTIPLIER = 5.00

DATA = [
    (
        group,
        label,
        (
            percentage * PERCENTAGE_MULTIPLIER
            if percentage > PERCENTAGE_THRESHOLD
            else percentage
        ),
        time_taken * TIME_MULTIPLIER,
    )
    for group, label, percentage, time_taken in DATA
]

COLORS = {
    "CanNM": "#2E7DD7",
    "DiagApp": "#F97316",
    "PDM": "#EC5FAE",
    "PNG": "#344B66",
    "PDC": "#EF4444",
    "DtcApp": "#7C3AED",
    "HwAbs": "#9A4C3F",
    "IoHw": "#2FA93C",
    "NmApp": "#21B7D9",
    "NvmApp": "#7A7A7A",
    "OperatingVoltage": "#B7B61A",
    "UDS": "#666666",
    "AFE": "#00A6A6",
    "altFee": "#C2410C",
    "fee": "#2563EB",
    "feeMgnt": "#9333EA",
}


GROUP_ORDER = [
    "CanNM",
    "DiagApp",
    "PDM",
    "PNG",
    "PDC",
    "DtcApp",
    "HwAbs",
    "IoHw",
    "NmApp",
    "NvmApp",
    "OperatingVoltage",
    "UDS",
    "AFE",
    "altFee",
    "fee",
    "feeMgnt",
]


# Preferred positions for the most crowded labels.
# Format: label -> (horizontal offset, vertical offset, alignment)
PREFERRED_POSITIONS = {
    "PDM_LocalCtrl_2": (-10, 10, "right"),
    "PDM_CANCtrl_2": (10, -18, "left"),
    "PDM_CANCtrl_1": (10, 5, "left"),
    "PDM_LocalCtrl_1": (10, 22, "left"),
    "IoHw_Dio": (8, -16, "left"),

    "Srv3E": (8, 11, "left"),
    "DiagApp": (8, -10, "left"),

    "Srv14": (8, 8, "left"),
    "Srv28": (8, -10, "left"),
    "Srv2F": (8, 8, "left"),

    "SrvBA": (8, 8, "left"),
    "Srv2E": (8, -8, "left"),

    "PDM_ProtectionCtrl_1": (8, 8, "left"),
    "PNG_Protection": (-8, 11, "right"),

    # Preferred positions for the new crowded AFE cluster.
    "afe_cellbalancing": (8, 10, "left"),
    "afe_diag_sm01": (-8, -12, "right"),
    "afe_diag_sm02": (8, -12, "left"),
    "afe_diag_sm03": (-8, -22, "right"),
    "afe_diag_sm04": (8, 8, "left"),
    "afe_diag_sm05": (8, 10, "left"),
    "afe_diag_sm06": (8, 10, "left"),
    "afe_diag_sm18": (-8, 9, "right"),
    "afe_diag_sm36": (8, 10, "left"),
    "afe_diag_sm37": (8, -10, "left"),
    "afe_diag_sm38": (-8, 10, "right"),
    "afe_diag_sm40": (8, 10, "left"),
    "afe_diag": (8, -10, "left"),
    "afe_registerBank": (8, 8, "left"),
    "afe_routine": (8, 8, "left"),
    "afe": (8, -8, "left"),
    "altFee": (8, 8, "left"),
    "fee": (8, 8, "left"),
    "feeMgnt": (8, 8, "left"),

}


def overlap_area(first: Bbox, second: Bbox) -> float:
    """Return the overlapping pixel area of two bounding boxes."""
    left = max(first.x0, second.x0)
    bottom = max(first.y0, second.y0)
    right = min(first.x1, second.x1)
    top = min(first.y1, second.y1)

    return max(0.0, right - left) * max(0.0, top - bottom)


def expand_bbox(box: Bbox, padding: float) -> Bbox:
    """Return a bounding box expanded by the specified pixel padding."""
    return Bbox.from_extents(
        box.x0 - padding,
        box.y0 - padding,
        box.x1 + padding,
        box.y1 + padding,
    )


def generate_candidate_positions(label: str):
    """
    Generate possible label positions around a point.

    Positions are measured in display points, so the algorithm also works
    correctly when the x-axis uses a logarithmic scale.
    """
    candidates = []

    if label in PREFERRED_POSITIONS:
        candidates.append(PREFERRED_POSITIONS[label])

    # Test progressively more distant positions around the dot.
    for distance in (7, 12, 18, 26, 36, 48):
        diagonal = distance * 0.65

        candidates.extend([
            (distance, 0, "left"),
            (distance, diagonal, "left"),
            (distance, -diagonal, "left"),

            (-distance, 0, "right"),
            (-distance, diagonal, "right"),
            (-distance, -diagonal, "right"),

            (0, distance, "center"),
            (0, -distance, "center"),
        ])

    # Remove duplicate candidate positions.
    unique_candidates = []
    seen = set()

    for horizontal, vertical, alignment in candidates:
        key = (
            round(horizontal, 2),
            round(vertical, 2),
            alignment,
        )

        if key not in seen:
            seen.add(key)
            unique_candidates.append(
                (horizontal, vertical, alignment)
            )

    return unique_candidates


def place_labels_without_overlap(
    figure,
    axis,
    label_information,
) -> None:
    """
    Automatically place labels while avoiding other labels and markers.

    This is implemented directly with Matplotlib and does not require the
    external adjustText package.
    """
    figure.canvas.draw()
    renderer = figure.canvas.get_renderer()
    axis_bounds = axis.get_window_extent(renderer)

    point_positions = [
        axis.transData.transform((item["time"], item["percentage"]))
        for item in label_information
    ]

    # Place labels in crowded areas first.
    for index, item in enumerate(label_information):
        current_point = point_positions[index]

        item["density"] = sum(
            1
            for other_index, other_point in enumerate(point_positions)
            if other_index != index
            and np.linalg.norm(current_point - other_point) < 105
        )

    ordered_labels = sorted(
        label_information,
        key=lambda item: (
            -item["density"],
            -len(item["label"]),
        ),
    )

    occupied_label_boxes = []

    for item in ordered_labels:
        annotation = axis.annotate(
            item["label"],
            xy=(item["time"], item["percentage"]),
            xytext=(7, 4),
            textcoords="offset points",
            fontsize=7.2,
            color=item["color"],
            horizontalalignment="left",
            verticalalignment="center",
            annotation_clip=False,
            zorder=4,

            # A small white background makes labels readable when they are
            # close to points or other colored text.
            bbox={
                "boxstyle": "round,pad=0.12",
                "facecolor": "white",
                "edgecolor": "none",
                "alpha": 0.84,
            },
        )

        best_candidate = None

        for (
            horizontal_offset,
            vertical_offset,
            horizontal_alignment,
        ) in generate_candidate_positions(item["label"]):
            annotation.set_position(
                (horizontal_offset, vertical_offset)
            )
            annotation.set_horizontalalignment(
                horizontal_alignment
            )

            label_box = annotation.get_window_extent(renderer)
            padded_label_box = expand_bbox(label_box, 2.5)

            # Prefer positions close to the corresponding point.
            score = (
                horizontal_offset ** 2
                + vertical_offset ** 2
            ) * 0.04

            # Strongly discourage labels from leaving the plot area.
            outside_distance = (
                max(0.0, axis_bounds.x0 - padded_label_box.x0)
                + max(0.0, padded_label_box.x1 - axis_bounds.x1)
                + max(0.0, axis_bounds.y0 - padded_label_box.y0)
                + max(0.0, padded_label_box.y1 - axis_bounds.y1)
            )

            score += outside_distance * 5000

            # Strongly discourage overlap with previously placed labels.
            for occupied_box in occupied_label_boxes:
                area = overlap_area(
                    padded_label_box,
                    occupied_box,
                )

                if area > 0:
                    score += 1_000_000 + area * 1000

            # Avoid placing a label on top of any scatter point.
            for point_x, point_y in point_positions:
                point_box = Bbox.from_extents(
                    point_x - 4.5,
                    point_y - 4.5,
                    point_x + 4.5,
                    point_y + 4.5,
                )

                area = overlap_area(
                    padded_label_box,
                    point_box,
                )

                if area > 0:
                    score += 100_000 + area * 100

            # Slightly prefer labels on the right side for most points.
            if (
                item["time"] < 700
                and horizontal_alignment == "right"
            ):
                score += 15

            if (
                best_candidate is None
                or score < best_candidate["score"]
            ):
                best_candidate = {
                    "score": score,
                    "horizontal_offset": horizontal_offset,
                    "vertical_offset": vertical_offset,
                    "horizontal_alignment":
                        horizontal_alignment,
                    "box": label_box,
                }

        annotation.set_position((
            best_candidate["horizontal_offset"],
            best_candidate["vertical_offset"],
        ))
        annotation.set_horizontalalignment(
            best_candidate["horizontal_alignment"]
        )

        occupied_label_boxes.append(
            expand_bbox(best_candidate["box"], 2.5)
        )

    figure.canvas.draw()


def create_scatter_plot(
    output_path: str | Path =
        "module_mcdc_vs_time_with_date.png",
) -> Path:
    """Generate the scatter plot with automatic label placement."""
    output_path = Path(output_path)

    # A wider image gives long labels more horizontal space.
    figure, axis = plt.subplots(
        figsize=(19, 12),
        dpi=170,
    )

    figure.patch.set_facecolor("white")
    axis.set_facecolor("white")
    axis.set_xscale("log")

    label_information = []

    for group in GROUP_ORDER:
        rows = [
            row
            for row in DATA
            if row[0] == group
        ]

        times = [row[3] for row in rows]
        percentages = [row[2] for row in rows]

        axis.scatter(
            times,
            percentages,
            s=36,
            color=COLORS[group],
            label=group,
            zorder=3,
        )

        for _, label, percentage, time_taken in rows:
            label_information.append({
                "label": label,
                "time": time_taken,
                "percentage": percentage,
                "color": COLORS[group],
            })

    # Calculate logarithmic x-axis limits from the transformed data.
    # This prevents points from being clipped when TIME_MULTIPLIER changes.
    positive_times = np.array(
        [
            time_taken
            for _, _, _, time_taken in DATA
            if time_taken > 0
        ],
        dtype=float,
    )

    if positive_times.size == 0:
        raise ValueError(
            "The logarithmic x-axis requires at least one positive time value."
        )

    minimum_time = float(positive_times.min())
    maximum_time = float(positive_times.max())

    # Multiplicative margins are appropriate for a logarithmic axis.
    # Start slightly before the smallest point without displaying 10^0.
    x_axis_minimum = max(minimum_time / 1.25, 1.01)
    x_axis_maximum = max(maximum_time * 1.40, 12000.0)

    # Display major logarithmic ticks from 10^1 through 10^4.
    major_log_ticks = [
        10.0,
        100.0,
        1000.0,
        10000.0,
    ]

    axis.xaxis.set_major_locator(
        FixedLocator(major_log_ticks)
    )
    axis.xaxis.set_major_formatter(
        LogFormatterMathtext(base=10)
    )

    # Extra space above 100% gives crowded labels room.
    axis.set_xlim(x_axis_minimum, x_axis_maximum)
    axis.set_ylim(-3, 108)
    axis.set_yticks(np.arange(0, 101, 10))

    axis.set_xlabel(
        "Time Taken (s)",
        fontsize=12,
    )
    axis.set_ylabel(
        "MC/DC Percentage",
        fontsize=12,
    )

    # Scatter plot only: no grid lines and no connecting lines.
    axis.grid(False)
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)
    axis.spines["left"].set_linewidth(1.1)
    axis.spines["bottom"].set_linewidth(1.1)
    axis.tick_params(
        axis="both",
        labelsize=10,
    )

    figure.text(
        0.055,
        0.955,
        "Module Accuracy vs Time Taken",
        fontsize=22,
        fontweight="bold",
        horizontalalignment="left",
    )

    figure.text(
        0.055,
        0.925,
        f" {date.today().strftime('%d %B %Y')}",
        fontsize=10.5,
        color="#555555",
        horizontalalignment="left",
    )

    legend_handles, legend_labels = (
        axis.get_legend_handles_labels()
    )

    figure.legend(
        legend_handles,
        legend_labels,
        loc="upper left",
        bbox_to_anchor=(0.052, 0.885),
        ncol=8,
        frameon=False,
        fontsize=9,
        handlelength=1,
        handletextpad=0.25,
        columnspacing=1.25,
    )

    # Finalize the chart area before measuring text positions.
    plt.tight_layout(
        rect=[0.035, 0.055, 0.98, 0.810]
    )

    place_labels_without_overlap(
        figure,
        axis,
        label_information,
    )

    figure.savefig(
        output_path,
        bbox_inches="tight",
        facecolor="white",
    )
    plt.close(figure)

    return output_path


if __name__ == "__main__":
    created_file = create_scatter_plot()
    print(
        f"Scatter plot created: "
        f"{created_file.resolve()}"
    )