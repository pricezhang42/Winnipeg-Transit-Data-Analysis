"""Static exploration of training observations, plus full-snapshot coverage."""

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def plot_overview(frame, config, output):
    train = frame.loc[frame["split"].eq("train") & frame["baseline_eligible"]]
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.titleweight": "bold", "axes.labelcolor": "#374151",
        "text.color": "#172033", "axes.edgecolor": "#CED4DE",
    })
    fig, axes = plt.subplots(2, 2, figsize=(16, 12), layout="constrained")
    fig.suptitle(f"{config['route']} departure delays\nCoverage of the download and patterns in the training period", fontsize=21)

    ax = axes[0, 0]
    valid_dates = frame["scheduled_date"].dropna()
    dates = pd.date_range(valid_dates.min(), valid_dates.max(), freq="D")
    counts = frame.groupby("scheduled_date").size().reindex(dates, fill_value=0)
    ax.bar(dates, counts.to_numpy(), color="#2563A6", width=.85)
    zeros = dates[counts.eq(0)]
    if len(zeros):
        ax.scatter(zeros, np.zeros(len(zeros)), marker="x", color="#C43E3E", s=55, zorder=3, label="No records")
    for key, color, label in [
        ("validation_start", "#137B74", "Validation starts"),
        ("test_start", "#C8771C", "Test starts"),
        ("test_end_exclusive", "#6B7280", "Reserved later dates"),
    ]:
        ax.axvline(pd.Timestamp(config[key]), color=color, linestyle="--", linewidth=1, label=label)
    ax.set_title("Daily BLUE records · entire download", loc="left", pad=12)
    ax.set_ylabel("Stop-departure observations")
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.tick_params(axis="x", rotation=30)
    ax.legend(frameon=False, fontsize=9, loc="upper left", bbox_to_anchor=(0, -.18), ncol=2)

    ax = axes[0, 1]
    minutes = train["delay_seconds"].astype(float) / 60
    outside = int((~minutes.between(-10, 30)).sum())
    ax.hist(minutes, bins=np.linspace(-10, 30, 101), color="#2563A6", alpha=.9)
    ax.axvline(0, color="#C43E3E", linewidth=1.4, label="Scheduled time")
    ax.axvline(minutes.median(), color="#137B74", linestyle="--", label=f"Median: {minutes.median():.2f} min")
    ax.set_xlim(-10, 30)
    ax.set_title("Delay distribution · eligible training rows", loc="left", pad=12)
    ax.set_xlabel(f"Departure delay (minutes; positive = late)\n{outside:,} training observations outside this display range")
    ax.set_ylabel("Stop-departure observations")
    ax.legend(frameon=False)

    ax = axes[1, 0]
    colors = {"Weekday": "#2563A6", "Saturday": "#C8771C", "Sunday": "#137B74", "Holiday": "#9961AD"}
    for day_type, group in train.groupby("day_type", observed=True):
        by_hour = group.groupby("hour", observed=True)["delay_seconds"].agg(["median", "count"])
        by_hour = by_hour.loc[by_hour["count"].ge(config["minimum_group_observations"])].reindex(range(24))
        ax.plot(by_hour.index, by_hour["median"] / 60, marker="o", markersize=3,
                label=day_type, color=colors.get(day_type, "#6B7280"))
    ax.axhline(0, color="#B8C0CC", linewidth=1)
    ax.set_title("Median delay by hour · eligible training rows", loc="left", pad=12)
    ax.set_xlabel(f"Scheduled local hour (America/Winnipeg)\nGaps indicate fewer than {config['minimum_group_observations']} observations")
    ax.set_ylabel("Median departure delay (minutes)")
    ax.set_xticks(range(0, 24, 3))
    ax.legend(frameon=False, ncol=2)
    ax.grid(axis="y", alpha=.15)

    ax = axes[1, 1]
    by_stop = train.groupby(["stop_number", "destination"], observed=True)["delay_seconds"].agg(["median", "count"])
    top = by_stop.loc[by_stop["count"].ge(100)].nlargest(10, "median").sort_values("median")
    short = {"Unicity Shopping Centre": "Unicity", "University of Manitoba": "U of M"}
    labels = [f"{stop} → {short.get(destination, destination)}" for stop, destination in top.index]
    bars = ax.barh(labels, top["median"].astype(float) / 60, color="#137B74", height=.65)
    for bar, count in zip(bars, top["count"]):
        ax.text(bar.get_width() + .035, bar.get_y() + bar.get_height() / 2,
                f"n={count:,}", va="center", fontsize=8, color="#596579")
    if not top.empty:
        ax.set_xlim(0, max(1, float(top["median"].max()) / 60) * 1.35)
    ax.set_title("Largest median delays by stop and destination", loc="left", pad=12)
    ax.set_xlabel("Median departure delay (minutes)\nEligible training rows; at least 100 observations per group")
    ax.tick_params(axis="y", labelsize=9)
    ax.grid(axis="x", alpha=.15)
    fig.savefig(output, dpi=150, facecolor="white")
    plt.close(fig)
