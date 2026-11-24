README phase 2

## Flujo de cohorte en la aplicación

Las cargas de BAM/BAI pueden repetirse para ir completando el roster. Cada carga valida y añade sus muestras, pero no lanza QC. Cuando se pulsa **Cerrar tanda y analizar muestras nuevas**, Nextflow procesa solo los BAMs nuevos. Tras finalizar, se puede abrir otra tanda; las muestras anteriores no se vuelven a procesar ni se pueden borrar. Python vuelve a calcular correlación, outliers, PCA y clustering usando todas las muestras analizadas.

Al finalizar, la pestaña **QC de muestras** presenta las métricas individuales y de cohorte, y permite guardar qué muestras se incluirán en el modelo. Las exclusiones son una decisión manual; los indicadores de QC no retiran muestras automáticamente. El estado del roster y la selección quedan guardados en `samples/cohort.yaml`.

a tener en cuenta:
- todos los workflows deben ser de nextflow
- Los workflows deben incluirse en una cola y solo correr cuando hay recursos disponibles
- debe haber un archivo de config de nextflow donde tu puedas indicar los recursos que das a cada proceso.
- los workflows deben correr con apptainer.
- Todos los parametros del GATK gCNV la app da un default, pero el usuario debe poder modificarlo en la propia app. Estos parámetros usados deben quedar guardados en un archivo yaml.

Una vez creado el bed, quiero que tu puedas subir bams analizados con este bed. Cada bam debe ser validado. Tu puedes subir los bams que prefieras en distintos batches. Una vez subidos los bams que quieres, se inicia un workflow donde se corre collectReadCounts, picard CollectHsMetrics y GATK Flagstat y análisis de python para poder hacer sample correlation, PCA y clustering outlier detection. Los analisis que se deben hacer por bam son los siguientes:

1. Métricas que puedes obtener directamente de CollectReadCounts
Primero, hay que tener en cuenta una característica importante de GATK: CollectReadCounts cuenta los inicios de las lecturas que caen dentro de cada intervalo, no calcula la profundidad media por base. Produce una matriz de recuentos enteros por intervalo, normalmente en HDF5. También aplica filtros internos: entre ellos, excluye lecturas duplicadas y lecturas que no superan el filtro de calidad de mapeo. GATK

Esto significa que puedes obtener bastantes métricas de QC sin ejecutar ningún programa adicional.
1.1. Métricas globales de read counts
Son las primeras que mostraría en la interfaz.
Métrica	Descripción	Utilidad
Total read counts	Suma de los recuentos de todos los intervalos	Estimar la cantidad global de señal disponible en las regiones analizadas.
Median counts per interval	Mediana de los recuentos por intervalo	Evaluar la cantidad típica de señal por región.
Mean counts per interval	Media de los recuentos por intervalo	Complementa la mediana y permite detectar distribuciones asimétricas.
P10 / P25 / P75 / P90 counts	Percentiles de la distribución de recuentos	Identificar muestras con una distribución global de cobertura desfavorable.
Zero-count intervals (%)	Porcentaje de intervalos con cero lecturas	Detectar regiones sin señal.
Low-count intervals (%)	Porcentaje de intervalos por debajo de un umbral configurable	Identificar muestras con muchos intervalos insuficientemente cubiertos.


Visualización recomendada:
- Histograma de read counts por intervalo.
- Boxplot de los recuentos por muestra.
- Tabla resumen de métricas por muestra.
- Distribución de intervalos con cero recuentos.
Un ejemplo de tabla:
Sample	Total counts	Median counts	Zero-count intervals	Low-count intervals
Sample_001	12.5 M	320	0.5%	1.2%
Sample_002	8.2 M	180	2.5%	5.3%
Sample_003	2.1 M	35	18.2%	25.4%


Los valores son ilustrativos.

Una muestra con muchos intervalos sin lecturas o con muy pocos recuentos puede introducir problemas en el entrenamiento del modelo.

Importante: no utilizaría un umbral universal de 10 lecturas para excluir muestras. GATK utiliza por defecto un umbral de 10 recuentos en FilterIntervals, pero se aplica a los intervalos: se eliminan aquellos que tienen menos de 10 recuentos en más del 50 % de las muestras. No significa que una muestra con un determinado porcentaje de intervalos por debajo de 10 deba excluirse automáticamente. GATK
1.2. Distribución de los recuentos entre intervalos
Esta es probablemente una de las partes más interesantes para evaluar la calidad de las muestras.

No basta con tener muchos read counts. También interesa que la señal esté distribuida razonablemente bien entre los intervalos.

Te recomendaría calcular:

A. Distribución de los recuentos por intervalo

Representación de la distribución de recuentos de cada muestra. Permite detectar:
- Muestras con muy poca señal.
- Distribuciones extremadamente asimétricas.
- Muestras con un número elevado de intervalos sin lecturas.
- Muestras con unos pocos intervalos que concentran una proporción desproporcionada de los recuentos.
B. Variabilidad de los recuentos normalizados

Aquí haría algo más sofisticado que calcular el coeficiente de variación de los recuentos brutos.

Los intervalos tienen características intrínsecas diferentes: longitud, contenido GC, mapeabilidad y eficiencia de captura. Por tanto, comparar directamente sus recuentos puede generar una interpretación errónea.

Te recomiendo normalizar los recuentos siguiendo un procedimiento similar al utilizado por DenoiseReadCounts:
1. Normalizar los recuentos de cada muestra respecto a su total.
2. Normalizar cada intervalo respecto a su comportamiento en el conjunto del cohorte.
3. Obtener ratios normalizados.
4. Transformarlos a escala log2.
GATK utiliza precisamente una estrategia de normalización basada en cobertura fraccional, medianas por intervalo y normalización por muestra antes de generar los log2 copy ratios. GATK

Con esta matriz normalizada puedes calcular:
- Median log2 ratio.
- Median absolute deviation (MAD).
- Robust standard deviation.
- Percentage of intervals with extreme normalized counts.
Una muestra con una dispersión muy elevada de los recuentos normalizados puede presentar una mayor variabilidad técnica y ser menos adecuada para entrenar el modelo.

Visualización recomendada: boxplots de los log2 ratios normalizados por muestra.
1.3. Correlación entre muestras
Implementaría esta funcionalidad obligatoriamente.

Una muestra que presenta un patrón de cobertura muy diferente al resto puede introducir variabilidad técnica en el modelo.

Calcularía la correlación de Pearson o Spearman entre las muestras utilizando los recuentos normalizados.

Recomendaría:
- Matriz de correlación entre muestras.
- Clustering jerárquico.
- Heatmap de correlaciones.
Ejemplo:
Sample	Mean correlation with cohort
Sample_001	0.97
Sample_002	0.95
Sample_003	0.72


Estos valores son ilustrativos.

Una muestra con una correlación considerablemente inferior a las demás debería revisarse.

No obstante, la correlación debe calcularse después de normalizar los recuentos y excluir los intervalos problemáticos. De lo contrario, las diferencias de longitud, GC o eficiencia de captura pueden dominar el resultado.
1.4. PCA de los read counts
También incorporaría PCA.

La matriz de entrada sería:
- Filas: muestras.
- Columnas: intervalos.
- Valores: read counts normalizados y transformados.
El objetivo sería detectar muestras que se alejan del comportamiento general del cohorte.

En la interfaz mostraría:
- PC1 vs PC2.
- PC1 vs PC3.
- Varianza explicada por cada componente.
- Identificador de cada muestra.
- Colores por batch, kit de captura o protocolo de secuenciación.
Esto permitiría detectar posibles efectos batch.

Hay que tener cuidado: una muestra puede separarse del resto por diferencias biológicas reales o por características técnicas. Por tanto, no utilizaría el PCA como criterio automático de exclusión.
2. Programas adicionales que recomiendo ejecutar sobre los BAM
Aunque CollectReadCounts proporciona información muy útil, no permite evaluar toda la calidad del BAM.

Yo incorporaría algunos programas adicionales.
2.1. Picard CollectHsMetrics (muy recomendado)
Para datos WES o paneles de captura dirigida, considero que es uno de los programas más interesantes.

CollectHsMetrics genera métricas de secuenciación y cobertura específicamente orientadas a experimentos de captura. broadinstitute.github.io

Las métricas más importantes serían:
Métrica	Descripción	Interpretación
MEAN_TARGET_COVERAGE	Profundidad media en las regiones diana	Cantidad global de cobertura.
MEDIAN_TARGET_COVERAGE	Profundidad mediana en las regiones diana	Cobertura típica de las regiones capturadas.
PCT_TARGET_BASES_10X	Proporción de bases con cobertura ≥10X	Porcentaje de regiones con cobertura mínima.
PCT_TARGET_BASES_20X	Proporción de bases con cobertura ≥20X	Cobertura de regiones a una profundidad mayor.
PCT_TARGET_BASES_30X	Proporción de bases con cobertura ≥30X	Evaluar la uniformidad de cobertura a mayor profundidad.
ZERO_CVG_TARGETS_PCT	Proporción de targets sin cobertura	Identificar regiones completamente descubiertas.
FOLD_80_BASE_PENALTY	Variabilidad de la cobertura	Evalúa la uniformidad de cobertura.
PCT_EXC_DUPE	Porcentaje de bases excluidas por duplicación	Cuantificar el impacto de los duplicados.
PCT_EXC_MAPQ	Porcentaje de bases excluidas por baja calidad de mapeo	Evaluar problemas de alineamiento.
PCT_EXC_OFF_TARGET	Porcentaje de bases fuera de los targets	Evaluar eficiencia de captura.
GC_DROPOUT	Pérdida de cobertura asociada al contenido GC	Identificar problemas de cobertura dependientes del GC.
AT_DROPOUT	Pérdida de cobertura asociada a regiones AT	Identificar problemas de cobertura dependientes del contenido AT.


Fuente: documentación de métricas de Picard. broadinstitute.github.io

¿Por qué es interesante para GATK gCNV?

Porque permite evaluar características que afectan directamente a la calidad de los datos de entrada:
- Una muestra con una cobertura media elevada, pero una distribución muy irregular, puede proporcionar información de peor calidad que otra con menor profundidad y cobertura más uniforme.
- Un porcentaje elevado de bases fuera de los targets indica una menor eficiencia de captura.
- Un elevado GC_DROPOUT puede introducir sesgos en regiones con contenido GC extremo.
El FOLD_80_BASE_PENALTY es especialmente interesante para evaluar la uniformidad de cobertura.

Lo incorporaría en el QC de tu aplicación.
2.2. GATK FlagStat o samtools flagstat
También ejecutaría un análisis básico del alineamiento.

Puedes utilizar GATK FlagStat o samtools flagstat. No necesitas ejecutar ambos.

Métricas interesantes:
Métrica	Descripción
Total reads	Número total de lecturas del BAM.
Mapped reads (%)	Porcentaje de lecturas alineadas.
Properly paired (%)	Porcentaje de lecturas correctamente emparejadas.
Duplicate reads (%)	Porcentaje de lecturas marcadas como duplicadas.
Secondary alignments	Alineamientos secundarios.
Supplementary alignments	Alineamientos suplementarios.
QC-failed reads	Lecturas que no superan los filtros de calidad de secuenciación.


GATK FlagStat proporciona estadísticas de alineamiento, duplicación y lecturas correctamente mapeadas, entre otras. GATK

Una consideración importante: CollectReadCounts excluye las lecturas marcadas como duplicadas, pero no identifica duplicados por sí mismo.

Por tanto, tienes que asegurarte de que los BAM se han procesado previamente con marcado de duplicados (por ejemplo, MarkDuplicates). No volvería a ejecutar MarkDuplicates únicamente para obtener métricas si el BAM ya está correctamente procesado.
2.3. Mosdepth (alternativa interesante)
También consideraría mosdepth.

Es especialmente útil si quieres disponer de información detallada de cobertura por región.

Permite calcular:
- Profundidad media por intervalo.
- Distribución de cobertura.
- Porcentaje de bases con cobertura ≥1X, 10X, 20X, 30X, etc.
- Cobertura por cromosoma.
- Cobertura por gen o región definida en un BED.
Por ejemplo:
mosdepth \
    --by targets.bed \
    --thresholds 1,10,20,30 \
    sample \
    sample.bam

Produce información de profundidad y del número de bases que alcanzan cada umbral en cada región.


