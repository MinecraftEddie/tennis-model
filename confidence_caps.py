"""
Cap on total data-availability contribution to the confidence score.

Data availability = data_source bonus + surface_n bonus + ytd bonus (both players).
These signals reflect how well-documented the players are, not how good the pick is.
Capping them prevents well-known players from automatically reaching HIGH confidence.
"""

DATA_AVAILABILITY_CAP = 0.55


def cap_data_availability(total_data_score: float) -> float:
    """
    Clamp the combined data-availability score to DATA_AVAILABILITY_CAP.

    Args:
        total_data_score: raw sum of data_source + surface_n + ytd bonuses

    Returns:
        min(total_data_score, DATA_AVAILABILITY_CAP)
    """
    return min(total_data_score, DATA_AVAILABILITY_CAP)
