# Glosario

Las palabras del proyecto, en el sentido en que se usan aquí.

| Palabra | Qué quiere decir |
|---|---|
| **🎯 (recomendado)** | El pick que sí se sugiere jugar. Hoy: favorito fuerte según Pinnacle, -500 o mejor en PlayDoit, liga con pago anticipado ([08](08-picks-y-parlays.md)) |
| **Pick** | Lo que más le gusta al programa de un partido. Todo partido analizado tiene uno |
| **Momio americano** | +128 = ganas 128 por cada 100; -140 = apuestas 140 para ganar 100. Es como lo muestra PlayDoit y como se muestra todo al dueño |
| **Momio mínimo** | El momio a partir del cual vale la pena el pick ("apuesta si paga X o mejor") |
| **De cada 100** | Una probabilidad dicha en palabras: "gana 65 de cada 100 partidos así" |
| **Las casas** | Las casas de apuestas europeas del feed de The Odds API. Su promedio hace de precio de PlayDoit |
| **Pinnacle** | La casa más precisa. Sin su margen es la referencia de "la probabilidad real" |
| **Sin margen / de-vig** | Quitar lo que la casa se queda (el overround) para sacar la probabilidad |
| **Cierre** | El último momio antes del partido. Solo vale si se descargó cerca del kickoff |
| **CLV** | Closing Line Value: si el precio tomado fue mejor que el del cierre. La señal más rápida de ventaja |
| **Sombra (shadow)** | Candidatas que no se juegan pero se guardan con su precio, su cierre y su resultado para aprender |
| **Modo recolección** | Desde el 25-sep-26: el programa no registra apuestas propias, solo junta picks y sombra |
| **Pago anticipado (PA)** | Promoción de PlayDoit: si tu equipo va 2 goles arriba, la apuesta se paga como ganada |
| **Bono de parlay** | PlayDoit paga extra sobre la ganancia de un parlay de 3+ piernas a -500 o mejor (5% a 35%) |
| **Pierna** | Cada apuesta dentro de un parlay |
| **Stale / anulada** | Una fila que ya no vale (un horario reprogramado, una apuesta sin datos): no cuenta ni gana ni pierde |
| **Reprogramado / pospuesto** | Partido que cambió de fecha o no se jugó; el horario viejo se anula |
| **Closing (cierre)** | El modo que corre cada 30 min: cuotas, alineaciones, picks y avisos |
| **Morning / evening / weekly** | Las corridas de la mañana, la noche y el lunes |
| **Watchdog** | El vigilante que avisa si algo dejó de correr |
| **Dixon-Coles** | El modelo de goles por equipo (ataque, defensa, local y la corrección de marcadores bajos) |
| **Ensemble** | La mezcla de modelos (Dixon-Coles, Poisson, Elo, xG, entre-ellos, forma) |
| **xG** | Goles esperados por la calidad de las ocasiones (Understat/FotMob, o estimado con tiros de ESPN) |
| **Peso del modelo (ancla)** | Cuánto creerle al modelo frente a Pinnacle, aprendido cada semana |
| **Sesgo por lado** | La corrección aprendida que le falta al modelo para local, empate y visitante, por región |
| **`model_state`** | La tabla donde vive todo lo aprendido |
| **Modo ahorro / esencial** | El freno del plan gratis de Neon: menos cierres y pasos al acercarse al límite del mes |
| **CU** | Unidad de cómputo de Neon. La base va fija en 0.25 CU |
| **Transferencia** | Lo que el programa descarga de la base. El plan gratis da 5 GB al mes |
| **Errores tolerados** | Errores que no tumbaron una corrida pero se avisan |
| **Apostando 50** | Toda ganancia o pérdida se dice como si se apostaran 50 a cada pick |
