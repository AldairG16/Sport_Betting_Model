# Los tests unitarios no tocan la DB. Si DB_URL no está configurada
# localmente, settings.py lanza RuntimeError al importarse — damos un
# valor dummy (igual que hace .github/workflows/tests.yml).
import os

if not os.environ.get("DB_URL"):
    os.environ["DB_URL"] = "postgresql://dummy:dummy@localhost/dummy"
