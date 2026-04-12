from sklearn.isotonic import IsotonicRegression
import numpy as np
import joblib


class ProbabilityCalibrator:

    def __init__(self):
        self.models = {
            "home": IsotonicRegression(out_of_bounds='clip'),
            "draw": IsotonicRegression(out_of_bounds='clip'),
            "away": IsotonicRegression(out_of_bounds='clip')
        }

    def fit(self, y_true, probs):
        """
        y_true: lista de resultados (0=home,1=draw,2=away)
        probs: dict con listas de probabilidades del modelo
        """

        y_home = [1 if y == 0 else 0 for y in y_true]
        y_draw = [1 if y == 1 else 0 for y in y_true]
        y_away = [1 if y == 2 else 0 for y in y_true]

        self.models["home"].fit(probs["home"], y_home)
        self.models["draw"].fit(probs["draw"], y_draw)
        self.models["away"].fit(probs["away"], y_away)

    def predict(self, prob_dict):
        return {
            k: float(self.models[k].predict([prob_dict[k]])[0])
            for k in prob_dict
        }

    def save(self, path="calibrator.pkl"):
        joblib.dump(self.models, path)

    def load(self, path="calibrator.pkl"):
        self.models = joblib.load(path)