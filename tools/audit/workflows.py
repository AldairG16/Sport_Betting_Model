"""Lectura estatica de los workflows de GitHub Actions con PyYAML.

Los entry points reales del sistema viven en `.github/workflows/*.yml`: si el
conjunto de comandos extraidos aqui esta incompleto, el analisis de
alcanzabilidad (modulos huerfanos) reporta basura. Un workflow nuevo debe
ampliar el conjunto automaticamente, sin tocar ningun checker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

__all__ = ["WorkflowInfo", "read_workflows", "workflows_dir"]


@dataclass
class WorkflowInfo:
    """Lo que un checker necesita saber de un workflow, sin ejecutarlo."""

    path: str
    name: str
    crons: list[str] = field(default_factory=list)
    run_commands: list[str] = field(default_factory=list)
    env_keys: set[str] = field(default_factory=set)


def workflows_dir(root: Path) -> Path:
    """Ubicacion canonica de los workflows dentro del repositorio."""
    return Path(root) / ".github" / "workflows"


def _mapa(valor: object) -> dict:
    """Devuelve el valor si es un mapping; si no, un dict vacio."""
    return valor if isinstance(valor, dict) else {}


def _clave_on(documento: dict) -> object:
    """Recupera el bloque `on:` del workflow.

    PyYAML interpreta la clave `on` como el booleano True (YAML 1.1), asi que
    hay que buscar ambas formas o los crons se pierden en silencio.
    """
    if "on" in documento:
        return documento["on"]
    if True in documento:
        return documento[True]
    return None


def _crons(documento: dict) -> list[str]:
    bloque = _clave_on(documento)
    if isinstance(bloque, str) or bloque is None:
        return []
    programaciones = _mapa(bloque).get("schedule")
    if not isinstance(programaciones, list):
        return []
    salida = [
        str(entrada["cron"])
        for entrada in programaciones
        if isinstance(entrada, dict) and "cron" in entrada
    ]
    return sorted(salida)


def _recolectar_env(contenedor: object, acumulador: set[str]) -> None:
    """Suma las claves de un bloque `env:` al acumulador."""
    for clave in _mapa(_mapa(contenedor).get("env")):
        acumulador.add(str(clave))


def _leer(ruta: Path, raiz: Path) -> WorkflowInfo:
    documento = _mapa(yaml.safe_load(ruta.read_text(encoding="utf-8")))

    comandos: list[str] = []
    env_keys: set[str] = set()
    _recolectar_env(documento, env_keys)

    for job in _mapa(documento.get("jobs")).values():
        _recolectar_env(job, env_keys)
        pasos = _mapa(job).get("steps")
        if not isinstance(pasos, list):
            continue
        for paso in pasos:
            paso_mapa = _mapa(paso)
            _recolectar_env(paso_mapa, env_keys)
            comando = paso_mapa.get("run")
            if isinstance(comando, str) and comando.strip():
                comandos.append(comando.strip())

    return WorkflowInfo(
        path=ruta.relative_to(raiz).as_posix(),
        name=str(documento.get("name") or ruta.stem),
        crons=_crons(documento),
        run_commands=comandos,
        env_keys=env_keys,
    )


def read_workflows(root: Path) -> list[WorkflowInfo]:
    """Un WorkflowInfo por archivo en `.github/workflows/`, ordenado por ruta."""
    raiz = Path(root)
    carpeta = workflows_dir(raiz)
    if not carpeta.is_dir():
        return []
    archivos = [
        ruta
        for patron in ("*.yml", "*.yaml")
        for ruta in carpeta.glob(patron)
        if ruta.is_file()
    ]
    return sorted(
        (_leer(ruta, raiz) for ruta in archivos),
        key=lambda info: info.path,
    )
