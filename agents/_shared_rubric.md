## Rúbrica y Criterios Compartidos de Evaluación (Tech Team Standards)

### 1. POLÍTICA DE EVIDENCIA ESTRICTA (Evidence-First)
- Ningún hallazgo puede presentarse como hecho sin evidencia concreta y verificable en código/configuración.
- Formato de evidencia requerido: `archivo:línea` o sección específica + explicación concreta de lo observado.
- Diferenciar rigurosamente entre:
  - **Hecho (Fact)**: Verificado directamente en el código o archivo de configuración.
  - **Inferencia (Inference)**: Consecuencia lógica deducida a partir de hechos concretos.
  - **Pregunta Abierta / Hipótesis**: Dudas sobre intención de negocio que requieren confirmación humana.
- Para afirmaciones de ausencia (ej. "no existe X"): Indicar los archivos inspeccionados y el alcance de la búsqueda.

### 2. CLASIFICACIÓN DE PRIORIDADES (P0 / P1 / P2)
- **P0 — Release Blocker Real**:
  - La aplicación o servicio no puede compilar, arrancar o inicializar.
  - Flujo de negocio crítico completamente roto sin alternativa.
  - Vulnerabilidad crítica de seguridad confirmada (ej. auth bypass, inyección).
  - Pérdida o corrupción de datos.
  - Migraciones o dependencias impiden el despliegue a producción.
  - *Nota*: La falta de documentación NO es P0 a menos que impida demostrablemente arrancar/configurar el sistema en cualquier entorno.
- **P1 — Esencial para V1 Sólida**:
  - Requisito funcional clave de V1 incompleto o con validaciones faltantes.
  - Falta de tests esenciales en rutas/componentes críticos.
  - Documentación obligatoria de inicio/configuración/APIs ausente.
  - Manejo deficiente de errores en flujos importantes.
  - Problemas severos de mantenibilidad o rendimiento que comprometen la estabilidad de la v1.
- **P2 — Mejora Recomendable / Deuda Técnica No Bloqueante**:
  - Refactorizaciones de código limpio.
  - Optimizaciones secundarias o documentación auxiliar.
  - Mejoras cosméticas de interfaz o estilo.

### 3. NIVELES DE CONFIANZA (Confidence)
- **high**: Evidencia directa, inequívoca y verificable en el código fuente.
- **medium**: Evidencia razonable respaldada por patrones del proyecto e inferencia técnica.
- **low**: Hipótesis preliminar o evidencia incompleta que requiere validación humana adicional.

### 4. COMPLETITUD Y POLÍTICA DE ZERO-FINDINGS
- Si tras auditar los archivos asignados a tu especialidad no detectas problemas reales sustentados con evidencia concreta, devuelve `"findings": []`.
- Devolver una lista vacía `findings: []` es la respuesta correcta y preferida frente a crear hallazgos artificiales.
- **ESTRICTAMENTE PROHIBIDO**: Generar objetos de hallazgo con campos vacíos `""` o placeholders como `"N/A"`, `"none"`, `"unknown"` o `"sin información"`.
- Cada hallazgo debe tener completos y significativos sus campos: `title`, `evidence`, `impact`, `recommendation`, `priority`, `confidence` y `files`.
