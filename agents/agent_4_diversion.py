class DiversionEngine:
    def __init__(self, threshold):
        """
        Initialize with the minimum DFC interaction count threshold (e.g., 2 or 3).
        """
        self.threshold = threshold

    def decide(self, interaction_count):
        """
        Decides if the OD pair is eligible for EWDFC based on interaction count.
        """
        eligible = "YES" if interaction_count >= self.threshold else "NO"
        return {
            "Eligible": eligible,
            "Interaction Count": interaction_count,
            "Threshold Applied": self.threshold
        }
