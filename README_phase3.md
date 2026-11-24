a tener en cuenta:
- todos los workflows deben ser de nextflow
- Los workflows deben incluirse en una cola y solo correr cuando hay recursos disponibles
- debe haber un archivo de config de nextflow donde tu puedas indicar los recursos que das a cada proceso.
- los workflows deben correr con apptainer.
- Todos los parametros del GATK gCNV la app da un default, pero el usuario debe poder modificarlo en la propia app. Estos parámetros usados deben quedar guardados en un archivo yaml.


Una vez subidos los bams, tu debes poder seleccionar cuales son los bams para incluir en tu modelo y generar la creación del modelo. El usuario debe poder escoger si correr filter intervals y debe correr determine germline contig ploidy y Germline CNV caller. Luego quiero que se puedan analizar métricas del modelo: Una vez has ejecutado FilterIntervals, DetermineGermlineContigPloidy y GermlineCNVCaller en modo COHORT, el siguiente paso es evaluar si has obtenido un modelo de cobertura estable, capaz de explicar la variabilidad técnica de las muestras y adecuado para analizar nuevas muestras en modo CASE.

Para tu aplicación, considero que esta debería ser una de las secciones más importantes: un apartado de Model Quality Assessment.

Hay que distinguir cuatro aspectos:
1. Calidad del modelo de ploidía generado por DetermineGermlineContigPloidy.
2. Convergencia y estabilidad del modelo de GermlineCNVCaller.
3. Capacidad del modelo para explicar la variabilidad de los read counts.
4. Calidad de las predicciones de número de copias y de los copy ratios.
No existe una única métrica que permita afirmar que un modelo gCNV es bueno. Es necesario combinar estas dimensiones.
1. Calidad del modelo de ploidía: DetermineGermlineContigPloidy
Antes de evaluar el modelo principal, comprobaría que las estimaciones de ploidía son coherentes.

GATK genera dos directorios principales:
- ploidy-model: parámetros del modelo de ploidía del cohorte.
- ploidy-calls: estimaciones de ploidía y parámetros específicos de cada muestra.
Hay dos archivos especialmente interesantes.
1.1. Contig ploidy y PLOIDY_GQ
En contig_ploidy.tsv puedes obtener la ploidía estimada y la calidad de la estimación para cada contig.

Mostraría:
Métrica	Interpretación
Ploidy por cromosoma	Número de copias estimado por contig.
PLOIDY_GQ	Calidad de la estimación de ploidía.
Discordancia de ploidía	Diferencias respecto al cariotipo esperado.
Ploidy outliers	Muestras con patrones de ploidía diferentes al resto.


Visualizaciones:
- Heatmap de ploidía por muestra y cromosoma.
- Distribución de PLOIDY_GQ.
- Tabla de muestras con ploidías inesperadas.
Por ejemplo, si tienes un cohorte de muestras con cariotipos XX y XY, deberías poder comprobar que las estimaciones de X e Y son coherentes con estos cariotipos.

Es importante considerar las regiones pseudoautosómicas (PAR), especialmente en paneles dirigidos, porque su representación puede afectar a la estimación de ploidía de los cromosomas sexuales. GATK
1.2. Global read depth
El archivo global_read_depth.tsv contiene información de profundidad media y ploidía media por muestra.

Mostraría:
- Global read depth por muestra.
- Distribución de profundidad del cohorte.
- Ploidía media estimada.
- Muestras con profundidades anómalas.
Esto permite comprobar si existe una gran heterogeneidad de profundidad entre las muestras que forman el modelo.
2. Convergencia y estabilidad del modelo: GermlineCNVCaller
Esta es una de las partes que considero más importantes para tu aplicación.

GATK ejecuta un proceso de inferencia probabilística mediante aproximaciones variacionales (ADVI), con optimización iterativa de los parámetros del modelo.

En modo COHORT, se estiman simultáneamente los parámetros del modelo de cobertura y los estados de número de copias.

Además de los directorios -model y -calls, GATK genera un tercer directorio:

-tracking

Este directorio contiene información sobre el proceso de ajuste del modelo (model fitting). GATK
2.1. Training convergence
Incorporaría una visualización de la evolución del entrenamiento.

Las métricas interesantes son:
Métrica	Utilidad
Number of training epochs	Número de épocas necesarias para completar el entrenamiento.
ADVI iterations	Número de iteraciones de inferencia variacional.
Training SNR	Señal-ruido utilizada para evaluar la convergencia.
Convergence status	Comprobar si el entrenamiento alcanza el criterio de convergencia.
Inference divergence	Detectar problemas de divergencia durante la inferencia.


GATK dispone de parámetros específicos para evaluar la convergencia mediante el SNR de entrenamiento:
- convergence-snr-averaging-window
- convergence-snr-countdown-window
- convergence-snr-trigger-threshold
Por ejemplo, el umbral por defecto de convergence-snr-trigger-threshold es 0.1. El algoritmo utiliza este criterio para iniciar la comprobación de convergencia durante el entrenamiento. GATK

Visualización que implementaría:

Un gráfico de líneas:
- Eje X: iteraciones de entrenamiento.
- Eje Y: Training SNR.
- Línea horizontal: umbral de convergencia configurado.
También mostraría un indicador:
- Converged.
- Not converged.
- Inference diverged.
No confundiría alcanzar el número máximo de épocas con haber convergido. Son situaciones distintas.

Importante: el formato exacto de los archivos del directorio -tracking puede depender de la versión de GATK y de gcnvkernel. Por tanto, implementaría un parser adaptado a la versión del workflow, en lugar de asumir nombres de columnas fijos.
3. Capacidad del modelo para explicar la variabilidad: Unexplained variance
Esta es, probablemente, la métrica cuantitativa más interesante para evaluar la calidad del modelo y su compatibilidad con nuevas muestras.

GATK estima una varianza residual no explicada por el modelo (unexplained variance). Esta representa la variabilidad de los read counts que no consigue explicar el modelo después de considerar los factores de cobertura y los estados de número de copias.

Hay dos dimensiones que conviene investigar.
3.1. Sample-specific unexplained variance (ψs)
Es una de las métricas que priorizaría.

GATK genera estimaciones de la varianza no explicada específica de cada muestra.

Su utilidad es especialmente importante cuando posteriormente quieras utilizar el modelo para muestras nuevas en modo CASE.

La documentación de GATK recomienda precisamente comparar la magnitud de esta varianza en las nuevas muestras con el rango observado en las muestras que generaron el modelo para evaluar su compatibilidad. GATK

Visualizaciones que implementaría:
1. Boxplot de unexplained variance por muestra.
2. Distribución de la unexplained variance del cohorte.
3. Ranking de muestras según su unexplained variance.
4. Detección de outliers.

También: 3.2. Interval-specific unexplained variance (ψj)
También estudiaría la variabilidad no explicada asociada a los intervalos.

La pregunta que queremos responder es:

¿Existen regiones genómicas para las que el modelo tiene dificultades sistemáticas para explicar los read counts?

Sería interesante identificar:
- Intervalos con elevada varianza residual.
- Genes con múltiples intervalos problemáticos.
- Regiones cromosómicas con una concentración de intervalos problemáticos.
Implementaría un gráfico Manhattan en el que cada punto represente un intervalo y el eje Y represente su varianza residual.

También añadiría un ranking de genes con mayor variabilidad residual.

Esto permitiría identificar regiones con problemas de mapeabilidad, cobertura o sesgos técnicos que no se han eliminado completamente con FilterIntervals.

Para tu aplicación, no mostraría todas las métricas en una única pantalla. Las distribuiría en cinco pestañas.
Pestaña	Contenido
1. Model Overview	Número de muestras, intervalos, shards, versión GATK, estado general del modelo.
2. Model Convergence	Training SNR, iteraciones, épocas, convergencia y errores de inferencia.
3. Model Parameters	Unexplained variance, GC bias curves y coverage bias factors.
4. Copy Ratio Analysis	Distribución de denoised copy ratios, MAD, histogramas y heatmaps.
5. CNV Calling Quality	Distribución de estados CN, probabilidades posteriores y calidad de segmentos.