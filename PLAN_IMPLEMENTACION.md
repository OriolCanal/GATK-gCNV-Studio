# Plan de implementación: aplicación para construir baselines GATK gCNV

## 1. Base del plan y alcance

Este plan sintetiza `README_general.md`, `README phase1.md`, `README_pahse2.md` y `README_phase3.md`. En el directorio no aparece `README_phase0.md`, aunque figure entre las pestañas del IDE; tampoco hay código de aplicación todavía. Por tanto, el plan ordena los requisitos disponibles y señala decisiones que habrá que cerrar antes de poner los workflows en producción.

El producto permitirá crear un proyecto por BED y referencia, caracterizar y validar ese diseño, cargar y evaluar BAMs en tandas, seleccionar muestras y generar modelos de cohorte de GATK gCNV. Guardará parámetros y resultados para que cada ejecución sea reproducible.

## 2. Lectura conjunta de los README

Los documentos describen un flujo coherente de extremo a extremo:

1. **Diseño de intervalos:** subir BED, elegir GRCh37/GRCh38, validarlo y ejecutar `PreprocessIntervals`/`AnnotateIntervals` (el README menciona `processIntervals`; concretar el nombre y la secuencia exacta según la versión de GATK elegida). Revisar intervalos originales y procesados, GC, mappability, duplicaciones segmentarias y filtrado.
2. **Incorporación de muestras:** añadir BAMs al roster del proyecto en una o varias cargas, comprobar requisitos de entrada y ejecutar QC por tandas. Cada tanda procesa solo las muestras nuevas; después Python recalcula correlación, outliers y demás métricas de cohorte sobre todas las muestras analizadas. La lista queda bloqueada mientras cada QC está activo. Una página QC permite revisar métricas y excluir muestras del modelo.
3. **Selección y entrenamiento:** elegir muestras del proyecto, configurar opcionalmente `FilterIntervals`, ejecutar `DetermineGermlineContigPloidy` y `GermlineCNVCaller` en modo COHORT.
4. **Evaluación del modelo:** mostrar ploidía, convergencia y tracking, varianza no explicada, copy ratios y calidad de las llamadas CNV.

Los requisitos transversales son Flask/Jinja/JavaScript, ejecución mediante Nextflow y Apptainer, cola de trabajos limitada por recursos, configuración de recursos por proceso y parámetros configurables con valores predeterminados persistidos en YAML.

## 3. Decisiones de diseño recomendadas

- **Aplicación web:** Flask para API/vistas y Jinja para páginas; JavaScript para gráficos interactivos. Mantener separadas las capas de rutas, dominio, persistencia, almacenamiento y ejecución.
- **Persistencia:** base de datos relacional (recomendación inicial: PostgreSQL para despliegue compartido; SQLite puede servir para desarrollo local). La base guarda metadatos, estado, parámetros y referencias a artefactos, no los ficheros grandes.
- **Ficheros:** raíz configurable con valor predeterminado `/ngs-results/GATK-gCNV-models/`. Estructura sugerida:

  ```text
  <storage>/<project_name>/
    bed/                         # originales y BED procesados
    bed_analysis/<job_id>/       # outputs de análisis y métricas
    samples/<sample_id>/bams/    # BAM/índice o referencia a ficheros gestionados
    samples/<sample_id>/qc/<job_id>/
    models/<model_id>/<job_id>/ # outputs, tracking, config y logs
  ```

- **Ejecución:** la aplicación crea trabajos persistidos y los entrega a un worker/cola. La cola controla concurrencia y recursos disponibles; Nextflow ejecuta los pipelines y Apptainer proporciona los contenedores. No asumir que Nextflow, por sí solo, implementa la cola de la aplicación. Los workers deben poder recuperar trabajos tras reinicios y registrar logs, exit status y versiones.
- **Reproducibilidad:** versionar los workflows, imágenes/contenedores y esquemas de parámetros. Guardar por ejecución un YAML inmutable con parámetros efectivos, referencias, versión de GATK/Nextflow, perfiles de recursos y muestras incluidas. Los valores editables deben validarse contra parámetros permitidos por la versión instalada.
- **Datos de entrada:** una entidad `Sample` representa la muestra biológica; `BamFile` representa uno o más ficheros de alineamiento asociados a la muestra y al proyecto BED. Registrar índice, tamaño/checksum, referencia, batch y estado de validación. Evitar copiar grandes BAMs si el entorno puede referenciarlos de forma segura.
- **Estados:** definir estados explícitos para validación y ejecución (`pending`, `queued`, `running`, `succeeded`, `failed`, `cancelled`) y conservar historial de reintentos.

## 4. Modelo de datos inicial

- `BedProject`: nombre, BED original, referencia, versión/configuración de análisis, estado y directorios.
- `BedAnalysis`: job, parámetros YAML, outputs, métricas agregadas, versión de herramientas y estado.
- `Sample`: identificador, metadatos opcionales (batch, kit, protocolo, sexo/cariotipo esperado) y proyecto BED asociado.
- `BamFile`: ruta o URI, índice, checksum, tamaño, validación, asociación a sample y batch.
- `SampleQC`: ejecución, outputs de `CollectReadCounts`/Picard/FlagStat y métricas parseadas.
- `CohortModel`: proyecto BED, nombre, selección de muestras, opciones de filtrado, referencia a parámetros y estado.
- `WorkflowJob`: tipo, prioridad, estado, recursos solicitados, parámetros efectivos, logs, outputs, timestamps, versiones y errores.
- `Artifact` (opcional como tabla común): tipo, URI/ruta, tamaño, checksum y relación con su entidad propietaria.

Las tablas de resultados detallados pueden crecer mucho. Mantener en la base métricas agregadas y metadatos; dejar matrices y tablas completas como artefactos descargables o consultables desde ficheros optimizados.

## 5. Fases de construcción

### Fase 0 — Cerrar especificaciones y contratos (especificación inicial completada)

- Elegir versiones soportadas de GATK, Nextflow, Apptainer y referencias; definir perfiles de ejecución (local/cluster) y cómo se suministran los recursos de referencia y tracks Umap por read length.
- Aclarar nomenclatura y secuencia de herramientas para BED: `PreprocessIntervals`, `AnnotateIntervals`, padding, filtrado, BED de targets y formatos de entrada/salida.
- Definir reglas de aceptación de BAM: BAM/CRAM, índice requerido, referencia compatible, RG, duplicados marcados, orden y validaciones.
- Definir límites configurables de recursos, concurrencia, almacenamiento y umbrales QC; no codificar criterios universales de exclusión de muestras.
- Documentar esquema de parámetros YAML y compatibilidad por versión de GATK.

**Entregable:** [ESPECIFICACION_FASE0.md](ESPECIFICACION_FASE0.md), con contratos de inputs/outputs de los tres workflows. Según las respuestas del usuario, el primer despliegue será local sin scheduler, concurrencia configurable iniciando en 1, acceso multiusuario desde red privada/de confianza sin login, flujo completo hasta modelo COHORT, bundles GRCh37/GRCh38 preparados y BAMs copiables o enlazables desde rutas permitidas. CPU/RAM, espacio/retención y rutas permitidas se definirán por operador. La instalación inventariará read lengths Umap y versiones de herramientas.

### Fase 1 — Esqueleto web y gestión de proyectos BED (implementación inicial)

- Crear Flask/Jinja, configuración por entorno, autenticación/autorización si el despliegue lo requiere, base de datos y migraciones.
- Crear y listar proyectos; cargar BED de forma segura, registrar referencia y validar formato, coordenadas, cromosomas y duplicados/solapamientos.
- Implementar workflow Nextflow de análisis de BED con contenedores Apptainer y persistencia de estado/logs.
- Parsear outputs y construir Overview, comparación BED original/procesado, distribución de longitud y cromosoma, GC, mappability y segmental duplication.
- Añadir tabla descargable de intervalos excluidos y motivos, además de gráficos con selector de cromosoma.

**Criterio de salida:** un BED válido produce un análisis reproducible y métricas navegables desde su proyecto.

**Avance:** creado el paquete Flask/Jinja, modelos iniciales, catálogo de referencias, carga y validación BED, cola/worker local, workflow Nextflow y vista de métricas/gráficos. Antes de considerar la fase operativamente validada hay que configurar los paths del catálogo, instalar dependencias y ejecutar el primer workflow real con Nextflow/Apptainer.

### Fase 2 — Carga de cohorte, QC conjunto y selección de muestras

- [x] Incorporar muestras y BAMs en varias cargas, asociarlos al BED y conservar metadatos de batch; la carga no inicia el QC.
- [x] Cerrar cada tanda con una acción explícita y ejecutar un job Nextflow solo para sus BAMs nuevos.
- [x] Permitir abrir una nueva tanda tras finalizar correctamente el QC anterior; conservar los QC previos y bloquear la lista mientras haya un workflow activo.
- [x] Recalcular correlación, outliers, PCA y clustering usando las nuevas muestras junto con todas las muestras previamente analizadas.
- [x] Validar existencia/lectura, índice, referencia, orden y consistencia de contigs; registrar checksum y errores entendibles.
- [x] Ejecutar workflow Nextflow de `ValidateSamFile`, `CollectReadCounts`, `CollectHsMetrics`, `FlagStat` y parseo Python.
- [x] Derivar métricas globales; calcular normalización, correlaciones, PCA y clustering/outliers sobre intervalos aptos.
- [x] Mostrar métricas individuales y de cohorte en una pestaña QC dedicada; permitir guardar explícitamente qué muestras usar en el modelo.

**Criterio de salida:** cada tanda QC queda rastreada; los QC individuales se conservan y las métricas de cohorte se recalculan con todas las muestras analizadas. La selección de muestras del modelo queda persistida.

**Implementación inicial:** `SampleBatch`/`BamSample`, copia o symlink restringido por `GATK_GCNV_BAM_ALLOWED_ROOTS`, workflow `workflows/sample_qc/main.nf`, QC incremental por tanda, CSV y panel de histogramas/correlación/PCA. Los grupos de carga son metadatos, no ejecuciones QC independientes. La selección se persiste en `samples/cohort.yaml`. CRAM y mosdepth no forman parte de esta entrega. `CollectHsMetrics` reutiliza los targets aceptados como aproximación. El umbral de conteo bajo queda fijado internamente en 10 y la configuración de ejecución se conserva en YAML.

### Fase 3 — Cola de trabajos y operación fiable

- [x] Un único worker local reclama jobs persistidos en orden y limita workflows concurrentes con presupuestos agregados configurables de CPU y RAM.
- [x] Definir recursos por label (`bed_analysis`, `sample_qc`) en `nextflow.config`, acotar `maxForks` por el presupuesto local de cada workflow y conservar los recursos efectivos en YAML.
- [x] Cancelar jobs en cola o Nextflow activo; el worker cierra grupos de procesos con SIGTERM y escala a SIGKILL si no terminan.
- [x] Gestionar apagado SIGINT/SIGTERM sin aceptar jobs nuevos y esperar que finalicen los workflows activos.
- [x] Recuperar jobs activos tras un cierre abrupto, parar procesos Nextflow huérfanos verificables, restaurar estados desde `run_metadata.json` y cerrar como fallidos los runs sin resultado comprobable.
- [x] Ofrecer limpieza en vista previa y eliminación explícita de directorios temporales antiguos, conservando outputs publicados y logs.
- [x] Mostrar el estado del worker en `/jobs` y mantener logs, report, timeline, trace, parámetros y metadatos por ejecución.
- [x] Permitir reejecutar desde `/jobs` los jobs `failed`, creando un snapshot/job nuevo, validando checksums y conservando el historial fallido; integrar los QC con el reintento de su lote vigente.

Una ejecución interrumpida se reanuda automáticamente con `-resume` una sola vez si la caché/sesión, los parámetros efectivos sin cambios y el work directory del job siguen disponibles, y el proceso huérfano se pudo detener y verificar como terminado. La recuperación busca también procesos Nextflow por el work directory único si el cierre abrupto ocurrió antes de guardar el PID. El worker no libera la reserva de CPU/RAM hasta confirmar que los procesos descendientes del workflow terminaron. Si no se cumple alguna condición, o falla de nuevo, queda fallida para revisión; los jobs que seguían en cola continúan al arrancar el worker. La cancelación de un QC actúa solo sobre las muestras incluidas en ese job.

**Implementación:** presupuesto por ejecución `GATK_GCNV_WORKFLOW_*`, presupuesto agregado `GATK_GCNV_WORKER_*_BUDGET`, techo `GATK_GCNV_MAX_CONCURRENT_WORKFLOWS`; limpieza mediante `flask --app run.py cleanup-nextflow-work` (preview) y `--apply`.

Los comandos GATK están separados en procesos modulares bajo `workflows/modules/local/gatk/`; `workflows/subworkflows/` compone los procesos de BED, QC y modelo COHORT. Las etiquetas de recursos se conservan en cada proceso para que `nextflow.config` limite cada tarea según su flujo.

**Criterio de salida:** la implementación limita la concurrencia por CPU/RAM, conserva trazabilidad tras recuperación y no reanuda hasta verificar la terminación del proceso previo y la integridad de los parámetros efectivos. La validación operativa con ejecuciones Nextflow concurrentes e interrupciones reales en el servidor sigue pendiente; no se considera completada por cambios de código solamente.

### Fase 4 — Construcción de modelos de cohorte

- [x] Permitir seleccionar muestras QC y congelar esa selección como snapshot del modelo, con rutas y checksums de read counts.
- [x] Generar YAML de parámetros efectivos con defaults visibles y editables; validar rangos y opciones antes de encolar.
- [x] Ejecutar `FilterIntervals` opcional, `DetermineGermlineContigPloidy` y `GermlineCNVCaller` en COHORT.
- [x] Persistir outputs publicados, tracking/model/calls, comandos/logs/versiones, parámetros y relación exacta entre muestras, intervalos y referencia.
- [x] Presentar estado, errores, artefactos descargables y posibilidad de crear una ejecución nueva ligada a una revisión previa.

**Implementación inicial:** la entidad `CohortModelRun` conserva cada ejecución inmutable; cada envío crea un snapshot propio bajo `models/<run_id>/`. `workflows/cohort_model/main.nf` ejecuta las tres herramientas en orden usando read counts HDF5. Los priors por contig son editables y se validan contra el sequence dictionary; por defecto, X usa 0.4925 para ploidías 1 y 2, Y usa 0.4925 para 0 y 1, y cada estado restante recibe 0.005; los demás contigs conservan prior 0.98 para ploidía 2. Se valida un mínimo técnico de dos muestras para el modo COHORT de DetermineGermlineContigPloidy, y se rechaza la mezcla de kits/protocolos conocidos. El filtrado actual usa recuentos; el reporte lista intervalos retenidos/excluidos y solo atribuye una causa genérica de filtrado cuando GATK no ofrece un motivo por intervalo.

**Criterio de salida:** modelo reproducible ligado a una selección inmutable de muestras y configuración. Falta validar el workflow con el contenedor GATK 4.7.0.0 y bundles reales del servidor; los nombres/estructura publicados y los parámetros deben confirmarse con ese smoke run antes de considerarlo operativamente validado.

### Fase 5 — Model Quality Assessment

- [x] Overview de versión, referencia, muestras, intervalos, shards detectados y estado de disponibilidad.
- [x] Detectar tablas TSV publicadas de ploidía y global read depth; mostrar heatmap de ploidía, GQ, profundidad y exportaciones CSV.
- [x] Descubrir tracking, varianza, copy-ratio y tablas de llamadas sin fijar un único subdirectorio; mostrar resultados interpretables y dejar enlaces a los artefactos originales.
- [x] Distinguir `unknown`/convergencia/divergencia reportada, leer SNR del TSV o log cuando el formato sea interpretable y guardar los parámetros de convergencia; permitir DEBUG/INFO y advertir que alcanzar el máximo de épocas no prueba convergencia.
- [x] Persistir `model_quality.json` y resumen de parser en `run_metadata.json`; comunicar que la calidad no tiene una métrica binaria universal.
- [ ] Validar parser y nombres de outputs contra una ejecución real de GATK 4.7.0.0; ajustar tracking/SNR, épocas, umbrales y divergencia según esquema efectivo.
- [ ] Completar comparación con cariotipo/PAR, detección de outliers, unexplained variance por muestra e intervalo, ranking por gen/región y curvas/factores de sesgo.
- [ ] Analizar matrices HDF5 de copy ratios/estados/posteriores para histogramas, MAD y calidad de segmentos.
- [ ] Añadir gráficos y exportaciones específicas para ratios, posterior de CN y métricas de varianza cuando sus esquemas reales estén confirmados.

**Implementación inicial:** parser tolerante a TSV con cinco pestañas en la ficha del modelo, resumen Overview, heatmap de ploidía y barras de profundidad cuando hay datos. GATK `DEBUG` queda elegido por defecto y permite capturar la evolución SNR del log si el formato coincide con el parser; `INFO` sigue disponible. El reconocimiento de tracking/varianza es heurístico y no interpreta HDF5 ni garantiza que los nombres de columnas candidatos sean equivalentes entre versiones. La ausencia de artefactos se expone como parcial/desconocida y se pueden descargar los outputs originales.

**Criterio de salida:** el usuario puede valorar estabilidad y limitaciones del modelo antes de usarlo en casos.

### Fase 6 — Endurecimiento y preparación de despliegue

- Revisar permisos de ficheros, validación de rutas, nombres seguros, carga grande/fragmentada, cuotas y protección de datos genómicos.
- Añadir copias de seguridad de la base de datos y política de retención/restauración de artefactos.
- Documentar instalación, configuración, actualización de workflows/contenedores y operación de la cola.
- Añadir login y autorización por proyecto como fase posterior explícita; el MVP no los incluye según decisión actual.

## 6. Orden de implementación recomendado

Construir primero el esqueleto de datos y jobs junto con el flujo BED; usarlo para validar el contrato común de Nextflow, contenedores, parámetros y artefactos. Después incorporar QC BAM y selección de muestras. El entrenamiento de cohorte depende de que esos dos flujos produzcan intervalos procesados y matrices compatibles, por lo que debe venir después. El panel de calidad del modelo depende de los formatos reales de outputs y tracking de las versiones fijadas; conviene implementar sus parsers contra outputs de referencia antes de cerrar los gráficos.

## 7. Riesgos y puntos a resolver

1. **Fase 0 ausente:** solicitar/recuperar ese README si contiene decisiones ya tomadas.
2. **Nombres y versiones de herramientas:** los documentos alternan `processIntervals`/`PreprocessIntervals` y describen outputs cuya estructura puede variar; fijar versión y confirmar cada comando/formato.
3. **Read length y Umap:** definir cómo se selecciona el track adecuado al experimento y cómo se valida que concuerda con la configuración.
4. **Límites de recursos:** una cola requiere un backend concreto (ejecutor local o scheduler del centro); la configuración dependerá del entorno donde corra Nextflow.
5. **Tamaño/privacidad:** BAMs y matrices son grandes y contienen datos sensibles; almacenamiento, permisos, copias y retención son requisitos del despliegue.
6. **Calidad sin umbral único:** presentar evidencia y flags configurables, dejando decisiones de exclusión bajo control del usuario.
7. **Metadatos opcionales:** batches, kit, protocolo, sexo/cariotipo y plataforma mejoran la interpretación, pero hay que definir qué campos serán obligatorios.

## 8. Próximo paso

Antes de implementar, completar la Fase 0 con el entorno de ejecución objetivo y las versiones de referencia. Después empezar por el esquema de datos, carga/validación de BED y workflow de análisis BED, porque establece la estructura de proyecto que usarán el QC y los modelos.
