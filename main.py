"""
main.py
========
Punto de entrada directo del pipeline de predicciones.
Para automatizacion completa usa: python scripts/orchestrator.py --mode morning
"""

from src.pipeline.prediction_pipeline import run_prediction_pipeline
from src.models.save_bets import update_bet_results

if __name__ == "__main__":
    run_prediction_pipeline()
    update_bet_results()
