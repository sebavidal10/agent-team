## Estándares de Mejora de Código (Local Improvement Standards)

### 1. REGLA DE ORO: RESPETAR EL BLUEPRINT DEL PROYECTO
- Toda propuesta de mejora y todo parche de código DEBE apegarse estrictamente al stack, dependencias y estilo de código identificado en el `ProjectBlueprint`.
- **PROHIBIDO**: Proponer herramientas, librerías o frameworks que no existan en el proyecto a menos que la tarea explícitamente lo requiera.
- Si el proyecto usa TypeScript estricto, todo código nuevo debe tener tipos explícitos sin `any`.
- Si el proyecto usa funciones y hooks, no uses clases. Si usa Vitest, no uses Jest.

### 2. PARCHES DE CÓDIGO PRECISOS Y COMPLETOS (Unified Diff)
- Los parches deben ser sintácticamente válidos y seguir el formato diff unificado estándar:
  ```diff
  --- a/ruta/al/archivo.ts
  +++ b/ruta/al/archivo.ts
  @@ -10,6 +10,12 @@
    linea_sin_cambios()
  + linea_agregada()
  - linea_eliminada()
  ```
- No uses placeholders como `// ... resto del código ...` dentro de las funciones modificadas; proporciona el contexto necesario para que el parche sea aplicable limpiamente.
- Todo archivo nuevo propuesto debe tener su contenido completo.

### 3. PRIORIDAD Y ALCANCE QUIRÚRGICO
- Cada mejora debe resolver un problema concreto de alto impacto (seguridad, manejo de errores, tests esenciales, tipado, refactorización limpia o feature solicitada).
- Evita cambios cosméticos innecesarios en archivos no relacionados.
- Todo cambio debe dejar el proyecto en un estado funcional y coherente.
