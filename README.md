# Local Agent Team (v0.1.2)

Equipo multiagente local basado en **LangGraph + Ollama + Pydantic** para auditar repositorios y definir planes de acción concretos hacia una v1 terminable y sólida.

Esta versión es **estrictamente de solo lectura (READ-ONLY)** y deliberadamente segura:
- Lee un repositorio local sin modificarlo ni copiar su código fuente.
- No ejecuta comandos shell en el repositorio analizado.
- No modifica Git ni usa worktrees.
- No utiliza APIs externas ni envía telemetría.
- Presupuestos de contexto diferenciados por rol para evitar desbordes y alucinaciones.
- Forzado de formato JSON nativo con Ollama JSON Schema + 1 retry controlado.
- Parser de recuperación multietapa tolerante a fallos.

---

## 👥 Equipo de Agentes y Límites de Contexto

| Rol | Enfoque | Límite Caracteres | Límite Archivos |
| :--- | :--- | :--- | :--- |
| **Architect** | Arquitectura global, límites de capas, riesgos y alcance v1 | 55.000 chars | 40 archivos |
| **Backend** | Endpoints, modelos de datos, validación, auth, contratos y seguridad | 65.000 chars | 50 archivos |
| **Frontend** | Flujos de usuario, estados UI (loading/error/empty), componentes | 65.000 chars | 50 archivos |
| **Testing** | Cobertura de tests, casos límite y suite mínima para v1 | 50.000 chars | 40 archivos |
| **Docs** | README, especificaciones, `.env.example`, contratos y guías | 40.000 chars | 30 archivos |
| **Reviewer (Tech Lead)** | Deduplica hallazgos, detecta contradicciones y consolida plan v1 | 45.000 chars | 40 archivos |

---

## 🚀 1. Requisitos

- Python 3.11+
- Ollama en ejecución local (`http://localhost:11434`)
- Un modelo local descargado (por ejemplo, `qwen2.5-coder:7b` o similar)

Verificación rápida:
```bash
python3 --version
ollama list
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

Cada auditoría genera un directorio único y versionado en `output/`:

```text
output/run-YYYYMMDD-HHMMSS/
    ├── run.log                 # Log completo con métricas de observabilidad
    ├── manifest.json           # Metadatos del run (tiempos, archivos, conteos, statuses)
    ├── final-report.md         # Informe final consolidado del Tech Lead
    ├── context/                # Listado de archivos analizados por cada rol
    │   ├── architect-files.txt
    │   ├── backend-files.txt
    │   ├── frontend-files.txt
    │   ├── testing-files.txt
    │   └── docs-files.txt
    ├── reports/                # Reportes estructurados en formato JSON (Pydantic)
    │   ├── architect.json
    │   ├── backend.json
    │   ├── frontend.json
    │   ├── testing.json
    │   ├── docs.json
    │   └── reviewer.json
    └── markdown/               # Reportes individuales de cada agente en Markdown
        ├── architect.md
        ├── backend.md
        ├── frontend.md
        ├── testing.md
        ├── docs.md
        └── reviewer.md
```

---

## ⚡ 4. Ejecución de una Auditoría Real

### Comando CLI:
```bash
agent-team /RUTA/A/TU_REPOSITORIO \
  --goal "Auditar el proyecto y definir una ruta concreta para terminar una v1 sólida."
```

### O mediante módulo Python:
```bash
python -m agent_team.main /RUTA/A/TU_REPOSITORIO \
  --goal "Auditar el proyecto y definir una ruta concreta para terminar una v1 sólida."
```

---

## 🧪 5. Suite de Pruebas Automatizadas

Todos los tests son determinísticos y no requieren tener Ollama activo:

```bash
python -m unittest discover -s tests -v
```

Cubre:
- Validación y normalización de esquemas Pydantic (`Finding`, `AgentReport`, `ReviewerReport`).
- Extracción y reparación tolerante de JSON (fences, texto circundante, fragmentos, listas sin wrapper).
- Presupuesto de contexto por rol y métricas de candidatos descartados.
- Observabilidad en terminal (estados `valid`, `repaired`, `fallback`, spinner, progreso, `NO_COLOR`).
- Manejo de retries controlados (máximo 1 reintento sin loops infinitos).

---

## 🔮 Próximas Fases (v0.2+)

En futuras versiones con capacidades de escritura se incorporarán:
1. Git worktrees aislados por agente.
2. Herramientas de edición y ejecución controlada de tests / linters.
3. Ciclo de corrección iterativo con aprobación humana obligatoria antes de mergear.
