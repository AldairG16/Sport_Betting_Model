# src/scripts/run_bets.py
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.pipeline.prediction_pipeline import run_prediction_pipeline
import pandas as pd

def main():
    # Ejecuta todo el pipeline (features + modelos + betting)
    bets = run_prediction_pipeline()

    if bets is None or len(bets) == 0:
        print("No hay apuestas disponibles para los próximos partidos.")
        return

    bets_df = pd.DataFrame(bets)

    # Ordena por edge descendente y stake
    bets_df = bets_df.sort_values(by=["edge", "stake"], ascending=False)

    print("\n✅ APUESTAS LISTAS PARA COLOCAR:")
    print(bets_df[["match", "match_date", "market", "probability", "odds", "edge", "stake"]].to_string(index=False))

if __name__ == "__main__":
    main()