import pandas as pd


def analyze_file(filepath):

    df = pd.read_csv(filepath)

    avg_speed = float(df["speed"].mean())

    max_speed = float(df["speed"].max())

    consistency = round(
        100 - (df["speed"].std() / df["speed"].mean() * 100),
        2
    )

    return {
        "avg_speed": avg_speed,
        "max_speed": max_speed,
        "consistency": consistency
    }