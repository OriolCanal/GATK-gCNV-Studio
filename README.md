# GATK gCNV Baseline Studio

Aplicación Flask/Jinja para gestionar proyectos BED y preparar su análisis con Nextflow y Apptainer.

## Estado

Las fases 1–5 incluyen gestión/validación de proyectos BED, lotes BAM y QC, cola local, generación de modelos COHORT y evaluación de calidad. La fase 4 ejecuta `FilterIntervals` opcional, `DetermineGermlineContigPloidy`, `GermlineCNVCaller` y postprocesamiento por muestra mediante Nextflow. La fase 5 organiza la evaluación en Model Overview e Interval Diagnosis, con procedencia, parámetros, comparación de modelos, métricas de cobertura desde HDF5, anotaciones disponibles, ratios denoised reconocibles y exportación CSV. El análisis real requiere bundles de referencia y read counts QC compatibles, además de Nextflow, Apptainer y samtools disponibles en el servidor.

## Instalación local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Configura las variables directamente en `.env`; ese es el único archivo de entorno del proyecto.

Edita `config/references.yaml` y registra FASTA, FAI y sequence dictionary para GRCh37 y/o GRCh38. Añade los tracks Umap/segmental duplication con sus read lengths si están disponibles. Los ficheros solo aparecerán en el selector si las rutas obligatorias existen.

Por defecto la app usa `/ngs-results/GATK-gCNV-models/`. Para desarrollo, cambia `GATK_GCNV_STORAGE_ROOT` a un directorio escribible. Para PostgreSQL configura, por ejemplo, `DATABASE_URL=postgresql+psycopg://usuario:clave@servidor/base_datos`.

Cada proyecto nuevo crea su directorio usando el nombre del proyecto como primer nivel del storage:

```text
/ngs-results/GATK-gCNV-models/<nombre-del-proyecto>/
  bed/
  bed_analysis/<job_id>/
  models/<model_run_id>/
    contig_ploidy_priors.tsv
    execution/
    outputs/                 # model, calls, tracking, métricas y manifiesto
```

Los nombres deben ser únicos y no pueden contener `/`, `\\` ni caracteres de control. Los proyectos creados antes de este cambio conservan su carpeta existente basada en ID.

## Arranque

Al abrir un proyecto se muestra el tablero de fases: diseño del BED, muestras y QC, modelos COHORT y evaluación de calidad. Las fases que requieren resultados previos aparecen bloqueadas hasta que esos requisitos estén disponibles.

En una terminal inicia el worker:

```bash
flask --app run.py work
```

En otra terminal inicia Flask:

```bash
flask --app run.py run --host 127.0.0.1 --port 5000
```

La aplicación de esta fase no incorpora login. Según la especificación, el acceso inicial debe limitarse a una red privada/de confianza. No uses el servidor de desarrollo de Flask como servidor de producción.

### Organización modular de Nextflow

Los procesos de herramientas están en `workflows/modules/local/gatk/`, con un proceso independiente por comando GATK. Los subworkflows `workflows/subworkflows/{bed_analysis,sample_qc,cohort_model}.nf` componen esos módulos; los `main.nf` de cada flujo validan/normalizan parámetros y delegan la ejecución. Las etiquetas `bed_analysis`, `sample_qc` y `cohort_model` permanecen en los módulos para que `nextflow.config` aplique los presupuestos y límites `maxForks` a cada proceso.

La página `/jobs` agrupa ejecuciones en cola, activas y finalizadas, muestra si el worker está activo y permite abrir logs en vivo o cancelar jobs. Solo se permite un proceso worker por instalación (bloqueo del sistema operativo); ese worker puede ejecutar varios workflows si los límites lo permiten. Al recibir `Ctrl+C` o `SIGTERM`, deja de aceptar jobs nuevos y espera a que terminen los activos.

Los jobs finalizados (`succeeded`, `succeeded_with_parse_warnings`, `failed` o `cancelled`) muestran **Reejecutar** en `/jobs`. La acción crea una ejecución nueva con `-resume`, reutiliza la sesión Nextflow y el directorio `work` originales, y conserva los registros y artefactos anteriores; los modelos COHORT quedan enlazados como revisiones. Los QC solo se pueden reejecutar desde el job vigente del lote. Un análisis BED aceptado puede volver a ejecutarse en una carpeta aislada, pero la lista de intervalos ya aceptada no se reemplaza automáticamente. Se comprueba la disponibilidad e integridad de los inputs según el tipo de workflow antes de encolar.

Los jobs en cola permanecen en la base de datos y siguen cuando vuelve a arrancar el worker. Si el worker cae de forma abrupta, al siguiente arranque se buscan los Nextflow huérfanos y se detienen solo cuando su identidad puede verificarse. Si el cierre ocurrió antes de guardar el PID, se busca el proceso por el workflow y el work directory único del job. La recuperación espera a confirmar que el grupo terminó, incluso después de SIGKILL, y reconcilia el estado desde `run_metadata.json`. El worker tampoco libera CPU/RAM reservadas hasta confirmar que terminaron los procesos descendientes de un workflow. Una ejecución interrumpida se reanuda una sola vez con `-resume` únicamente si siguen disponibles la sesión/cache de Nextflow y el work directory, y los parámetros efectivos coinciden con su checksum guardado. Si no se puede demostrar esa condición o vuelve a interrumpirse, queda fallida con log y artefactos conservados para revisión.

## Configuración del workflow

Los workflows están en `workflows/`; el perfil `local` activa Apptainer y el executor local. Cada módulo en `workflows/modules/local/gatk/` declara `cpus`, `memory`, `time` y `maxForks` propios. Sus valores se configuran en `.env` con variables `GATK_GCNV_<NOMBRE_DEL_PROCESO>_{CPUS,MEMORY,TIME}`; por ejemplo `GATK_GCNV_COLLECT_READ_COUNTS_CPUS` y `GATK_GCNV_COLLECT_READ_COUNTS_MEMORY`. Los nombres corresponden a los procesos Nextflow en mayúsculas, como `FLAG_STAT` o `PREPROCESS_INTERVALS`. La app calcula el `maxForks` individual de cada proceso con esos recursos y `GATK_GCNV_WORKFLOW_CPUS` / `GATK_GCNV_WORKFLOW_MEMORY`. Nextflow además planifica tareas concurrentes según los recursos solicitados y los disponibles para el executor local.

El worker reserva recursos agregados antes de reclamar cada job: `GATK_GCNV_WORKER_CPU_BUDGET` y `GATK_GCNV_WORKER_MEMORY_BUDGET` limitan CPU/RAM simultáneas entre workflows, mientras `GATK_GCNV_MAX_CONCURRENT_WORKFLOWS` es el techo de concurrencia. Configura ambos presupuestos según el servidor y asegúrate de que el presupuesto por workflow cubra el proceso más exigente. Los recursos son solicitudes para el scheduler; en el executor local, el sistema operativo no impone por sí solo un límite rígido al uso real. Cada ejecución guarda en YAML los recursos efectivos y Nextflow genera log, report, timeline y trace en su directorio.

Antes de `AnnotateIntervals`, el workflow genera índices GATK para los tracks Umap y segmental duplication habilitados. Los índices se crean dentro del directorio de trabajo del proceso y no modifican los assets originales del bundle.

Cada BED admite un análisis de intervalos activo a la vez. Los resultados vigentes se guardan en `bed_analysis/current/`; al iniciar una repetición se eliminan los resultados anteriores, mientras que los logs históricos se conservan. Al aceptar el análisis, la app registra padding y bin length en `bed_analysis/accepted_analysis.yaml` y bloquea nuevas ejecuciones para ese proyecto.

## Fase 2: lotes BAM y QC

En la página de BAMs se pueden cargar varios pares BAM/BAI, copiarlos al storage o enlazarlos desde rutas existentes. En modo copia, cada par se transfiere secuencialmente; la página muestra el nombre del BAM actual, su progreso y el de todo el lote. La app termina de recibir/copiar y validar el lote antes de crear el job QC; si falla la carga o la validación, no se encola. `GATK_GCNV_MAX_UPLOAD_MB=0` desactiva el límite de tamaño de petición de Flask; un valor positivo lo fija en MB. Si se usa un proxy inverso, hay que aumentar también el límite del proxy (por ejemplo, `client_max_body_size` en nginx), porque ese límite puede rechazar la petición antes de que llegue a Flask. Para el modo symlink configura `GATK_GCNV_BAM_ALLOWED_ROOTS` como rutas separadas por `:` en Linux; el worker Flask debe tener permisos de lectura en esas rutas. Se requiere `samtools` en el host (`GATK_GCNV_SAMTOOLS`) para verificar estructura, cabecera, orden, índice y compatibilidad de contigs.

La aplicación deriva el sample ID de la parte anterior al primer punto del nombre BAM, tanto en modo copia como symlink (por ejemplo, `RB44312.bqsr.bam` produce `RB44312`). Exige un único valor `SM` en los read groups que coincida exactamente con ese ID, BAM ordenado por coordenadas, BAI accesible y contigs compatibles con la referencia y los intervalos aceptados. No admite CRAM. La interfaz informa que los duplicados deben estar marcados previamente; no los modifica.

El QC por muestra presenta el porcentaje de bases diana por debajo de 30X y 100X, calculado como `100 × (1 − PCT_TARGET_BASES_30X)` y `100 × (1 − PCT_TARGET_BASES_100X)` de `CollectHsMetrics`, `MEAN_TARGET_COVERAGE` y `FOLD_80_BASE_PENALTY`. La correlación de cobertura de cada muestra es la media de sus correlaciones de Pearson con las demás muestras, calculada sobre los log2 ratios de read counts normalizados en intervalos comparables. `COHORT_OUTLIER_SCORE` es unilateral: cuántas desviaciones estándar robustas queda esa correlación por debajo de la mediana del cohort; se marca para revisión a partir de 3 y queda vacío si hay menos de tres muestras comparables. Es una señal de revisión, no excluye muestras automáticamente. El formulario solicita los BAMs y sus BAI, más el modo de almacenamiento; el nombre del lote se asigna automáticamente. La interfaz informa que los duplicados deben estar marcados previamente, sin pedir una confirmación adicional. El umbral de conteo bajo permanece en el valor interno predeterminado de 10. El worker ejecuta `ValidateSamFile`, `CollectReadCounts` HDF5, `CollectHsMetrics` y `FlagStat` mediante `workflows/sample_qc/main.nf`, con los mismos límites de recursos configurables del perfil local. El workflow declara FASTA, FAI y sequence dictionary como entradas para que Nextflow los prepare en el directorio aislado de cada tarea. El informe de `ValidateSamFile` se conserva. `INVALID_TAG_NM` y `MATE_NOT_FOUND` quedan como incidencias visibles y no bloquean los colectores; otros errores de validación y cualquier fallo de los colectores sí detienen esa muestra. Revisa las incidencias mostradas antes de usar una muestra en el entrenamiento. En esta versión, `CollectHsMetrics` usa los targets aceptados como baits y sus métricas de captura son aproximadas. Ninguna muestra se excluye automáticamente. Tras completar una tanda QC se puede abrir otra: Nextflow procesa solo los BAMs nuevos y Python recalcula las métricas de cohorte sobre todas las muestras analizadas. FlagStat se muestra en los detalles por muestra y en el CSV. Si falla el workflow, la página del lote permite reintentar el QC usando los BAM/BAI ya guardados. El resumen muestra histogramas, correlación y PCA; la PCA excluye intervalos X/Y, resalta las muestras marcadas para revisión y se puede descargar como PNG/SVG y CSV de coordenadas. El mapa de calor se puede descargar como PNG/SVG y su matriz como CSV, además de la tabla QC completa.

Los lotes y salidas se guardan bajo `samples/batches/<batch_id>/`; el resumen conjunto se escribe en `samples/project_qc_summary.json`. La configuración completa de cada ejecución queda en YAML junto con sus logs e informes Nextflow. Instala las dependencias Python actualizadas con `pip install -r requirements.txt` antes de arrancar la aplicación y el worker.

## Fase 4: modelos COHORT

En **Modelos COHORT** puedes consultar y abrir las ejecuciones del proyecto o añadir una nueva. Para cada envío eliges sus propias muestras QC, nombre y parámetros GATK; cada envío crea una ejecución inmutable bajo `models/<model_run_id>/`, con parámetros científicos y de ejecución en YAML, tabla de priors de ploidía, checksums de inputs y snapshot de IDs, read counts, lotes, kit/protocolo y referencia. Una revisión con parámetros distintos crea otra ejecución y conserva la relación con la anterior.

El listado muestra estados legibles (en cola, en ejecución, finalizado o fallido) y enlaza cada ejecución con su detalle de métricas, evaluación y artefactos. Los modelos fallidos se pueden eliminar desde ese listado; se retiran también su job y sus archivos. Si otros modelos los usan como base de revisión, esos vínculos se reubican en el antecedente del modelo eliminado.

El formulario valida rangos, priors por contig, que todos los read counts usen el mismo interval list aceptado y que no haya kits o protocolos conocidos mezclados. Se exigen al menos dos muestras para inferir ploidía en modo COHORT. La tabla inicial usa prior autosómico 0.98 para ploidía 2; para X asigna 0.4925 a ploidías 1 y 2 y para Y asigna 0.4925 a ploidías 0 y 1, con 0.005 a los demás estados 0–4. Puedes modificar la tabla según los cariotipos esperados. El tamaño recomendable de la cohorte depende del contexto; la aplicación no impone un mínimo mayor que el requisito técnico del modo.

`workflows/cohort_model/main.nf` corre `FilterIntervals` opcionalmente sobre read counts, después `DetermineGermlineContigPloidy` y `GermlineCNVCaller --run-mode COHORT`, usando intervalos coherentes entre etapas. A continuación ejecuta `PostprocessGermlineCNVCalls` una vez por muestra, vinculando cada resultado con el índice de entrada de `GermlineCNVCaller`; publica VCF genotipados de intervalos y segmentos y ratios de copia desnormalizados en `postprocess/SAMPLE_<índice>/`. La decisión de filtrado se lee de `filter_options.enabled`, el mismo valor persistido en el YAML científico. El filtro ofrece umbral de conteo bajo y percentiles extremos editables. GATK no atribuye una causa individual a cada intervalo excluido en esta salida; la tabla indica `filtered_by_gatk_filter_intervals` como causa general. Los outputs publicados incluyen los directorios model/calls/tracking, resultados de postprocesado, reporte de intervalos, métricas, YAML y `run_metadata.json`. La ficha del modelo muestra estado, log en vivo y descargas. Las cachés de ArviZ, PyTensor/Theano y Matplotlib dentro del contenedor se redirigen al directorio de trabajo escribible del task de Nextflow.

Configura recursos con `GATK_GCNV_COHORT_MODEL_CPUS`, `GATK_GCNV_COHORT_MODEL_MEMORY` y `GATK_GCNV_COHORT_MODEL_TIME`. Valida el workflow con el contenedor y los bundles reales del servidor antes de usar datos productivos.

### Evaluación de calidad del modelo (fase 5)

La ficha COHORT ofrece dos páginas, Model Overview e Interval Diagnosis. La primera resume versionaje, parámetros efectivos, procedencia y comparación entre ejecuciones. La segunda valida el interval list original contra CollectReadCounts y presenta métricas por base y normalizadas, además de outputs GATK solo cuando su formato y escala se reconocen. Consulta las fórmulas, formatos, caché y límites en [README_diagnosticos_cohort.md](README_diagnosticos_cohort.md). Los indicadores exploratorios no excluyen datos ni certifican idoneidad clínica.

### Validación externa de modelos

La pestaña **Validación** mantiene separados los casos de validación de las muestras usadas para entrenar la cohorte. Puedes cargar pares BAM/BAI, registrar CNVs conocidas con una línea `chr start end type` por evento (`DEL`, `DUP` o `CNV`), seleccionar muestras y ejecutar un modelo completado en modo CASE. El flujo usa los modelos de cobertura y ploidía guardados por el modelo COHORT y genera métricas por muestra, llamadas VCF de intervalos/segmentos y `denoised_copy_ratios.tsv`.

El resumen muestra las llamadas detectadas, CNVs anotadas, coincidencias por solapamiento de coordenadas y tipo, y una correlación de Pearson entre los perfiles de recuentos normalizados del caso y la mediana de los perfiles de entrenamiento en los intervalos del modelo. El detalle muestra calidad/genotipo de las llamadas y el log₂ ratio por intervalo. La correlación y la coincidencia son métricas descriptivas del análisis y no sustituyen una evaluación clínica validada.

La primera ejecución descarga la imagen de GATK. Si el acceso a Docker Hub desde el worker no está disponible, puedes descargarla manualmente y configurar la app para usar el SIF local:

```bash
mkdir -p /ngs-results/apptainer-cache
APPTAINER_CACHEDIR=/ngs-results/apptainer-cache apptainer pull \
  /ngs-results/apptainer-cache/gatk-4.7.0.0.sif \
  docker://broadinstitute/gatk:4.7.0.0
```

En `.env`, define `GATK_GCNV_APPTAINER_CACHE=/ngs-results/apptainer-cache` y `GATK_GCNV_GATK_CONTAINER=/ngs-results/apptainer-cache/gatk-4.7.0.0.sif`; después reinicia la aplicación y el worker. El worker usa esa ruta tanto para el SIF como para la caché interna de Apptainer. Solo puede estar activo un proceso worker por instalación.

El catálogo de referencias y la raíz de storage se pueden mover con `GATK_GCNV_REFERENCE_CATALOG` y `GATK_GCNV_STORAGE_ROOT`. En despliegue, establece `SECRET_KEY`, limita el acceso de red y usa PostgreSQL.

### Operación y retención

El comando `flask --app run.py work` valida que los presupuestos alcancen para al menos una ejecución. Si hay trabajos pendientes y `/jobs` indica que el worker está detenido, vuelve a iniciarlo. Los jobs finalizados conservan sus logs y outputs publicados. Los directorios temporales de Nextflow (`work/`) se mantienen 30 días por defecto y se pueden inspeccionar antes de borrarlos:

```bash
flask --app run.py cleanup-nextflow-work
flask --app run.py cleanup-nextflow-work --apply
```

La segunda orden elimina únicamente directorios `work/` de jobs terminales anteriores al periodo configurado; nunca elimina BAM/BAI, outputs publicados ni logs. Cambia el periodo con `GATK_GCNV_JOB_WORK_RETENTION_DAYS` o con `--older-than-days`.
