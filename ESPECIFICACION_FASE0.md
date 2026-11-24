# Especificación y contratos — Fase 0

**Estado:** especificación inicial cerrada para empezar el esqueleto de la aplicación. Los valores de versiones son pins iniciales que deben verificarse en una ejecución de integración en el entorno objetivo antes de procesar datos reales. Las decisiones que dependen del administrador del servidor están marcadas como tales.

## 1. Decisiones de producto y plataforma

| Área | Decisión inicial |
|---|---|
| Aplicación | Flask + Jinja; JavaScript para gráficos; API interna JSON para estado y métricas. |
| Base de datos | PostgreSQL en despliegue compartido; SQLite permitido solo para desarrollo local. |
| Almacenamiento | Filesystem gestionado por la aplicación, configurable con default `/ngs-results/GATK-gCNV-models/`. No escribir datos de usuario bajo el directorio del código. |
| Carpeta del proyecto | El primer nivel bajo el storage usa el nombre validado del proyecto (único, sin separadores ni controles). Los IDs internos siguen siendo UUID y cada job conserva su subcarpeta propia. |
| Workflow | Nextflow DSL2, separado en `bed_analysis`, `sample_qc` y `cohort_model`; cada uno tiene inputs/outputs definidos abajo. |
| Cola | Cola persistente en la aplicación y worker(s). Primer despliegue en servidor local, sin scheduler externo; Nextflow usa executor `local`. Concurrencia inicial 1 workflow, configurable por operador a partir de CPU/memoria disponibles. |
| Contenedores | Apptainer/SIF para procesos; las imágenes y versiones se fijan por release del pipeline. Evitar tags `latest`. |
| Versiones iniciales | GATK `4.7.0.0` y Nextflow `24.10.6` como pins de partida; Apptainer `1.3.x` o posterior, con patch fijado por el despliegue. El build debe registrar versiones exactas y digest/checksum de cada imagen. Actualizar estos pins solo mediante cambio revisado y ejecución de integración. |
| Referencias | Los bundles genómicos están preparados. Registrar cada build disponible como bundle administrado con FASTA, `.fai`, sequence dictionary, intervalos/alias y checksum; no aceptar rutas arbitrarias desde formularios. Confirmar si están preparados ambos builds (GRCh37 y GRCh38) y sus versiones. No se hace liftover automático. |
| BAMs | Admitir dos modos: copiar el BAM/BAI al storage administrado o registrar una ruta existente del servidor mediante symlink. Las rutas existentes deben estar dentro de raíces permitidas configuradas por el operador; validar destino real, permisos y existencia del BAI. Eliminar un registro o proyecto nunca elimina el archivo de origen enlazado. |
| Usuarios | Primera entrega accesible a varios usuarios desde una red privada/de confianza, sin login. Diseñar entidades/rutas para añadir autenticación y autorización por proyecto posteriormente, pero no implementar login en el MVP. No exponer el servicio públicamente en esta fase. |
| Parámetros | Defaults versionados en el servidor; formulario expone solo los parámetros admitidos por el schema del pipeline. Se persiste el snapshot YAML exacto de cada ejecución. El usuario no puede introducir flags shell arbitrarios. |
| Identidad de muestra | `sample_id` debe ser único dentro de un proyecto BED; usar el mismo valor como nombre de muestra en todos los recuentos. Mantener el identificador original del read group (`SM`) y exigir consistencia. |

Los pins son reproducibilidad de software, no garantía de compatibilidad del entorno: hay que confirmar el digest OCI/SIF y ejecutar el smoke run con recursos, sistema de archivos y scheduler reales antes de producción. La documentación oficial de GATK publica actualmente la release 4.7.0.0 y sus artefactos; la compatibilidad gCNV y el parsing de outputs se validan por versión fijada ([releases GATK](https://github.com/broadinstitute/gatk/releases)). Nextflow debe fijar `process.cpus`, `memory`, `time` y el executor por configuración/perfil ([configuración de Nextflow](https://www.nextflow.io/docs/latest/config.html), [directivas de proceso](https://www.nextflow.io/docs/latest/reference/process.html)).

## 2. Reglas comunes de datos y nombres

- BED de entrada: BED3 o superior, coordenadas 0-based y half-open. Ignorar líneas `track`/`browser` y comentarios; rechazar registros truncados, coordenadas negativas, `end <= start`, cromosomas no presentes en el diccionario de referencia y BED vacío. Validar y reportar solapamientos, duplicados, intervalos fuera de rango y orden; no corregirlos silenciosamente.
- El intervalo canónico para GATK se genera desde BED contra el bundle de referencia y se guarda como Picard interval-list. BED original se conserva sin cambios. Toda conversión registra referencia y convenciones de coordenadas.
- No renombrar cromosomas implícitamente (`1` frente a `chr1`). Cada bundle declara aliases aceptados y el mapeo aplicado queda en el reporte; ambigüedades o colisiones son error.
- Archivos que entran en una ejecución son inmutables desde que el job pasa a `queued`. Registrar tamaño, SHA-256, URI/ruta y fecha de registro. Un cambio crea un nuevo objeto/versionado.
- Los IDs internos de proyecto/job son opacos. La carpeta superior del proyecto usa su nombre visible porque es un requisito de organización; se rechazan separadores, caracteres de control, duplicados y destinos preexistentes, y se mantiene confinada a storage root. Nunca se construyen rutas de archivos con nombres de muestra.
- Muestras con distintos kits/protocolos/plataformas no se mezclan automáticamente. El usuario puede asociar metadatos para evaluar compatibilidad; el modelo guarda snapshot de esa selección.

### Formato común del manifiesto de ejecución

Cada workflow recibe un `manifest.json` versionado. Ejemplo esquemático:

```json
{
  "schema_version": "1.0",
  "project_id": "prj_...",
  "job_id": "job_...",
  "reference_id": "GRCh38",
  "reference_bundle_version": "2026.1",
  "samples": [
    {"sample_id": "SAMPLE_001", "bam": "/staged/SAMPLE_001.bam", "bai": "/staged/SAMPLE_001.bam.bai", "batch": "batch_01"}
  ],
  "parameters_file": "parameters.yaml"
}
```

Las rutas son rutas de staging visibles desde el worker y los procesos. El resultado común `run_metadata.json` registra `schema_version`, `job_id`, nombre/versión del pipeline, commit, GATK/Nextflow/Apptainer, executor, parámetros efectivos, referencias/checksums, muestras, timestamps, exit status y lista de artefactos con checksum.

## 3. Contratos de workflows

### 3.1 `bed_analysis`

**Entradas requeridas:** BED original, FASTA + FAI + dict del bundle seleccionado. **Opcionales:** track Umap y track segmental duplication compatibles con build; el bundle especifica read length del Umap y checksum. No se permite elegir un track sin build/read length declarados. Los bundles existen en el entorno, pero hay que inventariar sus builds y assets al configurar la instalación.

**Pasos lógicos:** validación determinista del BED; conversión a interval-list; `PreprocessIntervals`; `AnnotateIntervals`; parseo de métricas. Parámetros iniciales configurables: `padding`, `bin_length` (0 para panel/exoma target sin binning, salvo selección explícita), track Umap/read length, track de duplicación y opciones admitidas por la versión de GATK.

`PreprocessIntervals` fusiona solapamientos, aplica padding y puede dividir en bins; `AnnotateIntervals` produce anotaciones GC y, opcionalmente, mappability y segmental duplication. Para AnnotateIntervals se mantiene `OVERLAPPING_ONLY` y defaults para padding/merging de argumentos comunes. Ver [PreprocessIntervals](https://gatk.broadinstitute.org/hc/en-us/articles/360045800812-PreprocessIntervals) y [AnnotateIntervals](https://gatk.broadinstitute.org/hc/en-us/articles/360041416652-AnnotateIntervals).

**Salidas obligatorias:** BED original; BED validado sin alteración; interval-list canónico; interval-list preprocesado; TSV anotado; `bed_metrics.json`; `validation_report.json`; `run_metadata.json`; logs Nextflow/GATK. Si hay error de validación no se ejecuta GATK.

**Métricas mínimas:** número de intervalos, bases totales, longitud media/mediana y distribución de longitud, conteo por cromosoma, solapamientos/duplicados, comparación input/preprocessed y estadísticas GC. Mappability/duplicación serán `not_available` si faltan tracks. Reportar con claridad que procesamiento/fusión/padding pueden cambiar número y longitud.

**Importante sobre filtrado:** este workflow no excluye intervalos por baja cobertura. `FilterIntervals` necesita recuentos de muestras y pertenece al workflow de modelo. Por ello, “retenidos/excluidos por FilterIntervals” se presenta junto a una ejecución de cohorte con su desglose y tabla descargable; la ficha BED solo muestra métricas estructurales y anotaciones.

### 3.2 `sample_qc`

**Entradas:** BED project con interval-list canónico compatible; cada BAM con BAI y bundle de referencia coincidente; manifiesto con `sample_id`, `batch` y archivo. Requerir orden coordinate-sorted y lectura por índice; comprobar contigs/diccionario, índice y que los `SM` de read groups no sean ambiguos. CRAM queda fuera del primer contrato y se añadirá como extensión cuando se confirme disponibilidad de referencia indexada y pruebas de compatibilidad.

**Pasos:** `CollectReadCounts` por muestra sobre intervalos canónicos, `CollectHsMetrics` usando targets, `FlagStat` y parser Python de outputs/matriz. Los BAM deben venir previamente marcados para duplicados; CollectReadCounts filtra duplicados marcados, no los descubre. Guardar de forma explícita filtros y opciones de cada herramienta. `mosdepth` queda como extensión opcional y no bloquea primera entrega.

**Salidas por muestra:** read counts HDF5 como formato canónico para entrada de ploidía/caller; TSV de resumen de métricas para inspección, output de CollectHsMetrics, output de FlagStat, JSON de métricas QC y logs. No exponer una opción de formato por job en la primera release. GermlineCNVCaller acepta HDF5 o TSV de CollectReadCounts ([GermlineCNVCaller](https://gatk.broadinstitute.org/hc/en-us/articles/4405451329179-GermlineCNVCaller)).

**Métricas derivadas:** suma/media/mediana/percentiles de counts; cero y low-count intervals con umbral mostrado/configurado, nunca como exclusión universal; correlación/PCA sobre matriz normalizada; metrics de CollectHsMetrics y FlagStat. Los valores faltantes se representan como `null` con motivo, no como cero.

**Interfaz:** estado y métricas por BAM, resumen por batch, histogramas/boxplots, correlación, PCA y tabla exportable. PCA/correlación se calculan sobre conjunto comparable de muestras/intervalos y se presentan como revisión, sin exclusión automática.

### 3.3 `cohort_model`

**Entradas:** un BED project; snapshot de muestras QC aprobadas/seleccionadas por usuario; todos los read-counts compatibles con el mismo intervalo, referencia y configuración; parámetros YAML; modo COHORT. Validar muestras duplicadas, matrices incompletas/incompatibles y mezcla de protocolos según metadatos antes de encolar.

**Orden:** `FilterIntervals` opcional sobre recuentos; `DetermineGermlineContigPloidy` en COHORT; `GermlineCNVCaller` en COHORT usando llamadas de ploidía y los intervalos filtrados cuando aplique. Mantener coherencia entre los intervalos pasados a los read counts, anotaciones, ploidía y caller. La documentación de GATK describe que el caller usa counts de CollectReadCounts y resultados de ploidía, y que COHORT aprende parámetros del conjunto; revisar compatibilidad de librería/preparación al formar el cohorte ([GermlineCNVCaller](https://gatk.broadinstitute.org/hc/en-us/articles/4405451329179-GermlineCNVCaller), [DetermineGermlineContigPloidy](https://gatk.broadinstitute.org/hc/en-us/articles/360036349252-DetermineGermlineContigPloidy)).

**Salidas obligatorias:** outputs completos de FilterIntervals si está activado; directorios model/calls de ploidía y caller; directorio tracking si la versión lo genera; listado de intervalos retenidos/excluidos y motivos cuando estén disponibles; métricas parseadas, logs, manifiesto y YAML efectivo. Los parsers incluyen `parser_version` y `gatk_version`; si el formato no coincide, el job no se declara fallido si GATK terminó correctamente, pero el estado de parsing será `partial` con artefactos crudos preservados.

La selección debe tener tamaño suficiente para el objetivo científico y consistencia técnica. No imponer un mínimo recomendado universal; mostrar advertencias contextuales. La interfaz sí exige dos muestras como mínimo técnico para inferir ploidía en COHORT: GATK indica que con una única muestra se requiere un modelo ploidía previo, lo que corresponde al modo CASE y no al entrenamiento de esta fase ([DetermineGermlineContigPloidy](https://gatk.broadinstitute.org/hc/en-us/articles/360041414512-DetermineGermlineContigPloidy)). La guía de GATK sugiere al menos 30 muestras para WES/WGS, pero el número recomendado depende de la profundidad, calidad y homogeneidad ([GermlineCNVCaller](https://gatk.broadinstitute.org/hc/en-us/articles/4405451329179-GermlineCNVCaller)).

## 4. Configuración YAML y límites de parámetros

Una ejecución guarda dos archivos: `parameters.yaml` (valores científicos efectivos) y `execution.yaml` (recursos/backend y opciones de ejecución). Ejemplo:

```yaml
schema_version: "1.0"
pipeline: cohort_model
pipeline_version: "0.1.0"
gatk_version: "4.7.0.0"
reference:
  id: GRCh38
  bundle_version: "2026.1"
intervals:
  filter_intervals: true
  filter_options: {} # se completa desde el schema de la versión fijada
ploidy:
  mode: COHORT
  options: {}        # allowlist generada/revisada para GATK fijado
gcnv:
  mode: COHORT
  options: {}
samples:
  - SAMPLE_001
```

No se debe considerar `options: {}` un catálogo final: antes de habilitar cada argumento hay que extraerlo del `--help` de la versión pinneada y clasificarlo (usuario, operador interno o no soportado). La primera allowlist de la fase 4 expone low/extreme-count options de `FilterIntervals`, escala de priors de ploidía y parámetros de convergencia/copy-number de `GermlineCNVCaller`; se valida contra GATK 4.7.0.0 durante la integración. Cada campo expuesto define tipo, default, rango, descripción y efecto en el modelo. Persistir YAML serializado y checksums al encolar; editar parámetros crea una nueva ejecución, nunca muta una ya iniciada.

`execution.yaml` contiene perfiles nombrados (`local`, futuro `slurm`), CPU/memoria/tiempo por proceso, límites de concurrencia, `workDir`, ubicación de SIFs y binds necesarios. Nunca incluir secretos ni tokens en YAML, logs o línea de comando registrada.

## 5. Cola, estado y fallo

Estados persistidos: `validating`, `pending`, `queued`, `running`, `succeeded`, `failed`, `cancel_requested`, `cancelled`, `succeeded_with_parse_warnings`. Transiciones auditadas con timestamps. El worker reclama jobs transaccionalmente; limite de concurrencia y recursos configurados evitan sobreasignación. Nextflow conserva work directory para trazabilidad/reanudación; outputs finales se publican en rutas inmutables por job.

No marcar como cancelado hasta confirmar el cierre del proceso. Reintentos explícitos crean intento nuevo vinculado al job padre y conservan logs/workdir anterior. La ejecución registra exit code de Nextflow y cada proceso. La reanudación automática queda deshabilitada en el primer release hasta confirmar condiciones de idempotencia y scheduler.

## 6. Validación de contrato y criterios de aceptación

Antes de dar Fase 0 por operativamente validada, usar fixtures sintéticos/no sensibles y un bundle de referencia de prueba:

1. BED válido de varios cromosomas genera interval-list y TSV anotado legibles, métricas con mismas coordenadas y metadatos de referencia correctos.
2. BED inválido da reporte por línea y no crea job GATK.
3. BAM + BAI con `SM` único da read counts y outputs QC parseables; faltas de índice/referencia/contig generan rechazo claro.
4. Un cohorte pequeño de prueba ejecuta la cadena en contenedor fijado; verificar nombres y estructura reales de outputs, parser, tracking y YAML reproducible. No extrapolar este smoke run como validación científica de potencia del modelo.
5. El job queda registrado aunque worker/servidor se reinicie; no se pierde el estado ni se sobrescriben outputs de otra ejecución.

No se incluyen comandos clínicos ni reglas automáticas de exclusión. Los umbrales de QC se muestran junto a sus valores y origen; ningún score sustituye la revisión del usuario.

## 7. Decisiones aún dependientes del entorno

Estas decisiones de despliegue aún deben confirmarse:

1. CPU/RAM disponibles, espacio real en `/ngs-results`, límite de tiempo por job y configuración de concurrencia (se inicia en 1).
2. Raíces de filesystem autorizadas para los symlinks, grupos/permisos Unix y política de retención para copias y originales.
3. Read lengths cubiertas por Umap y versiones exactas de Apptainer/Nextflow instaladas. Los bundles GRCh37 y GRCh38 están disponibles con assets completos.

Se implementa inicialmente con executor `local`, concurrencia 1, BAM con BAI, counts HDF5, contenedores SIF, sin login en la primera entrega y ambos modos de custodia de BAM sujetos a raíces permitidas. CPU/RAM, espacio, retención y rutas permitidas se configuran por operador. Los bundles GRCh37/GRCh38 están preparados; la instalación inventariará versiones y read lengths. El acceso sin login queda restringido a red privada/de confianza; login y autorización se incorporarán posteriormente.

## 8. Referencias primarias

- [GATK 4.7.0.0 releases](https://github.com/broadinstitute/gatk/releases)
- [PreprocessIntervals](https://gatk.broadinstitute.org/hc/en-us/articles/360045800812-PreprocessIntervals)
- [AnnotateIntervals](https://gatk.broadinstitute.org/hc/en-us/articles/360041416652-AnnotateIntervals)
- [CollectReadCounts](https://gatk.broadinstitute.org/hc/en-us/articles/360036889312-CollectReadCounts)
- [CollectHsMetrics / Picard](https://broadinstitute.github.io/picard/command-line-overview.html#CollectHsMetrics)
- [DetermineGermlineContigPloidy](https://gatk.broadinstitute.org/hc/en-us/articles/360036349252-DetermineGermlineContigPloidy)
- [GermlineCNVCaller](https://gatk.broadinstitute.org/hc/en-us/articles/4405451329179-GermlineCNVCaller)
- [Nextflow configuration](https://www.nextflow.io/docs/latest/config.html)
- [Nextflow process directives](https://www.nextflow.io/docs/latest/reference/process.html)
- [Apptainer bind paths](https://apptainer.org/docs/user/latest/bind_paths_and_mounts.html)
