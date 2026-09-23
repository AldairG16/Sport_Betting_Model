def mostrar_dashboard(
    home, away,
    probabilities,
    odds,
    home_form,
    away_form,
    bets
):

    print("\n" + "="*60)
    print(f"⚽ PARTIDO: {home} vs {away}")

    # =========================
    # PROBABILIDADES
    # =========================
    print("\n📊 PROBABILIDADES MODELO\n")

    for k, v in probabilities.items():
        print(f"{k}: {v*100:.1f}%")

    # =========================
    # ODDS vs FAIR ODDS
    # =========================
    print("\n💰 FAIR ODDS vs MARKET\n")

    for market, prob in probabilities.items():

        odd = odds.get(market)

        fair = 1 / prob if prob > 0 else None

        if odd:
            print(f"{market} | Market: {odd:.2f} | Fair: {fair:.2f}")
        else:
            print(f"{market} | Fair: {fair:.2f}")

    # =========================
    # TEAM FORM
    # =========================
    print("\n📈 TEAM FORM\n")

    if home_form:
        print(f"{home}")

        gpg = home_form.get("gpg", round(home_form.get("goals_scored", 0) / max(home_form.get("matches", 1), 1), 2))
        gcpg = home_form.get("gcpg", round(home_form.get("goals_conceded", 0) / max(home_form.get("matches", 1), 1), 2))

        print(f"GPG: {gpg} | GC: {gcpg}")
        print(f"Attack: {home_form.get('attack_rating')} | Defense: {home_form.get('defense_rating')}")

    print()

    if away_form:
        print(f"{away}")

        gpg = away_form.get("gpg", round(away_form.get("goals_scored", 0) / max(away_form.get("matches", 1), 1), 2))
        gcpg = away_form.get("gcpg", round(away_form.get("goals_conceded", 0) / max(away_form.get("matches", 1), 1), 2))

        print(f"GPG: {gpg} | GC: {gcpg}")
        print(f"Attack: {away_form.get('attack_rating')} | Defense: {away_form.get('defense_rating')}")

    # =========================
    # VALUE BETS
    # =========================
    print("\n🔥 VALUE BETS\n")

    if not bets:
        print("❌ No hay apuestas de valor")
    else:
        for bet in bets[:3]:
            print(
                f"{bet['market']} | edge: {round(bet['edge'],3)} | odds: {bet['odds']}"
            )

    print("="*60)