# Arranque optimizado para desarrollo local con PostgreSQL/Neon remoto.
# --nothreading: reutiliza la misma conexión persistente en vez de abrir una
# conexión por hilos efímeros del servidor de desarrollo.
# --noreload: evita que el autoreloader escanee constantemente un proyecto
# alojado dentro de OneDrive. VS Code puede seguir guardando archivos; reinicia
# este comando cuando quieras aplicar cambios de Python.

if (-not (Test-Path ".\venv\Scripts\python.exe")) {
    Write-Host "No encuentro venv. Activa/crea el entorno virtual primero." -ForegroundColor Yellow
    exit 1
}
& ".\venv\Scripts\python.exe" manage.py runserver --nothreading --noreload
