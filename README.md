# Local Improvement Team (v0.2.0)

Equipo multiagente local basado en **LangGraph + Ollama + Pydantic** diseñado para **analizar proyectos locales y generar planes de mejora con parches de código (`.diff`) listos para aplicar**.

Optimizado específicamente para modelos de código locales como **`qwen2.5-coder:7b`** ejecutándose en Ollama de forma 100% privada, sin enviar código a la nube ni modificar el repositorio sin tu consentimiento.

---

## 👥 Equipo de Agentes y Flujo de Trabajo

```
[Proyecto Local + Objetivo]
           │
           ▼
  1. 🔍 PROFILER (Discovery & Arquitectura)
     Analiza manifests y configuraciones para extraer el stack,
     frameworks y convenciones de código (Project Blueprint).
           │
           ▼
  2. 📋 PLANNER (Estratega de Mejoras)
     Cruza tu objetivo con el Blueprint y define 2 a 4 mejoras
     de alto impacto con archivos objetivo delimitados.
           │
           ▼
  3. 🛠️ BUILDER (Generador de Parches)
     Escribe el código real y genera los parches en formato
     unified diff (.diff / git diff) respetando el estilo del proyecto.
           │
           ▼
  4. 🧐 REVIEWER (Tech Lead & Control de Calidad)
     Valida la integridad de los parches y elabora la guía
     paso a paso para aplicarlos con 'git apply' y verificarlos.
```

---

## 🔒 Modo Asistido (Seguro y en Solo Lectura)

- **Inspección Segura**: Lee tu proyecto local en solo lectura. No modifica ningún archivo del repositorio ni crea ramas sin que tú lo decidas.
- **Parches Listos en `output/`**: Cada ejecución deposita los archivos `.diff` individuales en la carpeta `output/run-YYYYMMDD-HHMMSS/patches/`.
- **Control Total**: Tú decides qué parches aplicar revisándolos previamente o ejecutando `git apply`.
- **Zero Telemetría**: Ejecución 100% local con Ollama en `localhost:11434`.

---

## 🚀 1. Requisitos

- Python 3.11+
- Ollama en ejecución local (`http://localhost:11434`)
- Modelo descargado en Ollama (recomendado: `qwen2.5-coder:7b`):
  ```bash
  ollama pull qwen2.5-coder:7b
  ```

---

## 📦 2. Instalación

```bash
git clone <repo> agent-team
cd agent-team

python3 -m venv .venv
source .venv/bin/activate

pip install -U pip
pip install -e .

cp .env.example .env
```

---

## 📊 3. Estructura de Salida por Ejecución

Cada corrida genera un directorio versionado en `output/`:

```text
output/run-YYYYMMDD-HHMMSS/
    ├── run.log                 # Registro de tiempos, métricas y fases
    ├── manifest.json           # Metadatos del run (stack, mejoras, duraciones)
    ├── project-blueprint.md    # Radiografía técnica del proyecto (lenguaje, framework, convenciones)
    ├── improvement-plan.md     # Detalle de las mejoras planificadas
    ├── final-guide.md          # Guía paso a paso para aplicar y probar los cambios
    ├── reports/                # Reportes estructurados en formato JSON (Pydantic)
    │   ├── blueprint.json
    │   ├── plan.json
    │   ├── builder.json
    │   └── reviewer.json
    └── patches/                # Parches de código individuales listos para aplicar
        ├── patch-01-auth_controller.diff
        └── patch-02-validation_schema.diff
```

---

## ⚡ 4. Uso del Equipo

### Ejecución básica:
```bash
agent-team /ruta/a/tu-proyecto-local \
  --goal "Mejorar el manejo de errores y validaciones en las rutas de autenticación."
```

### Opciones avanzadas:
```bash
# Con modelo diferenciado para el Reviewer (ej. llama3.1:8b)
agent-team /ruta/a/tu-proyecto-local --reviewer-model llama3.1:8b

# Modo interactivo para confirmar la aplicación de parches en terminal
agent-team /ruta/a/tu-proyecto-local --interactive
```

### Cómo aplicar los parches generados:
```bash
# 1. En la carpeta de tu proyecto local, crea una rama de trabajo:
git checkout -b mejora-local

# 2. Aplica el parche generado por el equipo:
git apply /ruta/a/agent-team/output/run-YYYYMMDD-HHMMSS/patches/patch-01-auth_controller.diff

# 3. Revisa los cambios con git diff y corre tus tests:
git diff
npm test
```

---

## 🧪 5. Suite de Pruebas

Todas las pruebas son deterministas y no requieren tener Ollama activo:

```bash
python -m unittest discover -s tests -v
```
