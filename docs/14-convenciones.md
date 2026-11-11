# 14 · Convenciones y preferencias del dueño

## Lo que pidió el dueño

| Tema | Cómo se hace |
|---|---|
| **Idioma** | Todo en español. Con el dueño, en palabras de todos los días: "de cada 100", "las casas" (no "mercado"), "recomendado" (no "pasa la regla"), "ganancia" (no "unidades"). Nunca ROI, "u", "Unidades" ni "Rendimiento" en lo que él ve. Los reportes técnicos del domingo pueden ir en unidades |
| **Momios** | Siempre americanos (+128 / -140) con `src/utils/min_odds.py` (`fmt_american`, `to_american`, `min_american`). Los decimales solo dentro del código y la base. Las reglas se dicen "apuesta si paga X o mejor" |
| **Dinero** | "Apostando 50 a cada uno" (`telegram_reports.STAKE` y `STAKE` en `ui_es5.js`) |
| **Motivos** | Cada partido dice su motivo concreto ("no hay favorito claro", "Arsenal paga muy poco (-667)"), no la regla |
| **Picks** | Nunca se esconde un pick: los filtros solo marcan |
| **Pasos manuales** | Ninguno. Nada que el dueño tenga que escribir, registrar o confirmar |
| **Preguntas** | Cuando hay que decidir: preguntas numeradas en texto (1a/1b…), con datos y una recomendación, y se espera la respuesta. Sin ventanas de opciones |
| **Honestidad** | Decir claro cuando no hay evidencia o algo no funciona. No celebrar rebanadas con suerte; juzgar por CLV o valor contra Pinnacle con z ≥ 3 |
| **Plan gratis** | Todo tiene que funcionar con planes gratis; subir de plan es decisión (y compra) del dueño |

## Cómo se trabaja

- **Ramas:** `master` es producción. Todo cambio va en una rama y un PR a `master`; se une cuando pasan las
  pruebas de GitHub. Nunca auto-merge sin que el dueño lo pida.
- **Commits** con `Co-Authored-By: Claude …` y PRs con "🤖 Generated with Claude Code".
- **YAML de workflows y secretos** no se tocan sin permiso: un push a un YAML de producción lo dispara.
- **Datos:** ningún borrado sin respaldo antes; los arreglos de una vez (`scripts/fix_*`) corren en ensayo por
  defecto y guardan con `--apply`, con su tabla `*_backup`.
- **Secretos:** nunca se imprimen ni se copian. La dirección de la base (`DB_URL`) se filtra de toda salida.
- **Documentación:** el cambio que cambia cómo funciona algo actualiza su documento en el mismo PR. Una clase de
  error nueva se vuelve un chequeo ([12](12-auditoria-y-chequeos.md)) y una lección (15 a 17).
- **Código:** comentarios y textos en español, nombres de archivos y funciones en inglés. Los comentarios dicen
  la fecha y el motivo ("26-oct-26: el dueño pidió…").
- **Dashboard:** se instala solo con el procedimiento de [10](10-dashboard.md).
- **Scraping:** no se raspa PlayDoit ni se brincan filtros anti-bots (ClubElo, Altenar).

<!-- detalle movido de OPERACION.md -->

## Convenciones

El código está comentado en español (histórico del proyecto), con nombres de fichero y
función en inglés. Sigue ese estilo: comentarios en español; los mensajes de commit llevan
el prefijo en inglés (`fix:`, `feat:`, `docs:`) y el texto en español, igual que los títulos
de los PR.

`archive/` documenta la regla de **archivar, nunca borrar** para módulos que dejan
de ser alcanzables. Cada módulo retirado lleva su motivo, qué lo sustituye y si es
seguro re-ejecutarlo (`archive/README.md`).

### Tests

`tests/pipeline_harness.py` corre `run_prediction_pipeline()` de punta a punta sin DB
ni APIs (dependencias sustituidas por datos fijos). Los cambios de lógica se prueban
**ejecutando** el pipeline, no buscando texto en el código:
`tests/test_pipeline_end_to_end.py` congela la salida completa en
`tests/golden/pipeline_baseline.json`; si un cambio intencional la altera, se regenera
con `UPDATE_GOLDEN=1 python -m pytest tests/test_pipeline_end_to_end.py` y el diff va
en el commit. El escenario extendido (`extended_kwargs`) apaga el salto por falta de
Dixon-Coles (`MIN_DC_WEIGHT` = 0): sus equipos sintéticos no tienen ajuste a propósito, y
`tests/test_output_checks.py` vuelve a encenderlo para probarlo.

Ningún test busca texto en el código fuente (el 23-sep-2026 se convirtieron los que
quedaban). Uno de ellos exigía que `setdefault` y `player_club_goals` aparecieran en
`load_scorer_rates`, y pasaba mientras los goleadores de clubes se descartaban siempre.
Para el código que escribe en la base, `tests/fake_db.py` es un motor falso que
registra cada sentencia; las consultas se verifican sobre el SQL que de verdad se envía.

Dos redes de seguridad corren con la suite:

- `tests/test_static_analysis.py` exige cero hallazgos de pyflakes en `src/`,
  `scripts/`, `config/`, `dashboard/` y `tests/`: nombres sin definir, imports o
  variables sin uso.
- `tests/test_entrypoints.py` resuelve cada `from src|scripts|config|dashboard import x`
  del proyecto, también los que están dentro de funciones. Esos imports solo fallan
  cuando se ejecuta su rama, a veces una vez por semana y en producción.

### Ramas

- **`master`** — producción y rama por defecto. Es la que dispara el agente externo.
- **`main`** — rama de un build de blueprint: trae herramienta de auditoría estática
  (`tools/audit`), 288 tests y documentación de esquema, pero le faltan 8 de los 9
  workflows. **No es desplegable.**

---

**Textos en palabras normales (v2.9.2, 6-nov-26).** El dueño: "necesito que el texto que usas en el
dashboard sea más amigable, no entiendo nada eso de al favorito de 65%". Casi todas las tarjetas decían
"solo 🎯 al favorito de 65%+ con pago anticipado", también la del PSG de 83% o la del Arsenal de 75%: el
motivo guardado (`reason`) habla del pick que se ve (más/menos goles, ambos anotan) y no de por qué el
favorito no es 🎯. Ahora cada pick sin 🎯 guarda además `star_note` (`program_picks.no_star_note`, mismo
orden que `strong_favourite`): "No hay favorito claro" (nadie llega a 65 de cada 100 según Pinnacle),
"Rosenborg es muy favorito, pero esta liga no tiene pago anticipado", "Arsenal paga muy poco (-667)",
"No hay momio de PlayDoit para…" o el motivo de la alineación. Antes de los dos puntos va lo corto que
cabe en la tarjeta; lo largo sale al pasar el mouse. `reason` no cambia (lo leen `filter_learner`,
`board` y la liquidación), salvo el texto de `NOT_STRONG`, que nadie lee por su texto. Los picks de
antes de v2.9.2 no tienen nota y la página traduce su `reason` (`shortWhy` en `ui_es5.js`).

En la página: arriba de Próximos, qué es 🎯 en una línea; la barra dice "de cada 100"; cada tarjeta lleva
el pick, "sin 🎯: …" y el estado de la alineación en renglones aparte; el encabezado cuenta los 🎯 de hoy
(decía "🎯 Hoy: 6 picks" contando todos los picks, con cero 🎯); "pasan la regla" pasa a "recomendados",
"unidades" a "ganancia", ROI a "lo que se gana o pierde de cada 100 apostados", "mercado" a "las casas", y
el parlay explica cómo se arma y da la probabilidad en veces ("más o menos 1 de cada 7"). Lo cubre
`tests/test_friendly_texts.py`, que corre las funciones de la página con node.
v2.9.3 corrige el singular y el plural que se veían ya instalado: "Hoy: 1 partidos" y "⏳ falta las
alineaciones de todos" (ahora "1 partido" y "faltan las alineaciones… (salen ~1 h antes)").
Telegram usa las mismas palabras: el aviso de alineación dice "no es 🎯, no hay favorito claro: no lo
apuestes" (lo corto de `star_note`) en lugar de "no pasa la regla del programa (…)", y el del parlay
"Se gana más o menos 1 de cada 4 veces (según Pinnacle)" y ⚡ en lugar de "Salen todas: ~24%" y "PA".
El aviso de alineación pone 🎯 solo al pick recomendado; los demás van con 🤖 (decía "🎯 Pick… — no es 🎯").
