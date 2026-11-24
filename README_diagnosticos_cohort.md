# Diagnósticos de modelos GATK gCNV COHORT

La ficha del modelo presenta dos páginas principales: **Model Overview** e **Interval Diagnosis**. El overview incluye trazabilidad, flags efectivas de GermlineCNVCaller, comparación entre hasta cuatro ejecuciones y las vistas existentes de ploidía, muestras y tracking. Interval Diagnosis combina el interval list original, los read counts y los outputs de postprocesamiento solo cuando sus coordenadas y encabezados son compatibles. Se integra en la navegación del proyecto y no añade dependencias al frontend.

## Datos y procedencia

El parser valida el orden y las coordenadas exactas de los intervalos en los HDF5 de CollectReadCounts de todas las muestras. Rechaza coordenadas duplicadas y cualquier diferencia de intervalo u orden entre muestras. Usa `/counts/values` y los metadatos `/intervals` del formato HDF5 actual, con el lector existente como fallback para formatos anteriores.

Artefactos nativos que se leen cuando están publicados:

- `filtered.interval_list`: pertenencia al conjunto retenido por FilterIntervals.
- `annotated_intervals.tsv`: GC y mappability si están presentes.
- `postprocess/SAMPLE_*/denoised_copy_ratios.tsv`: valores denoised solo si el TSV declara `CONTIG`, `START`, `END` y `LOG2_COPY_RATIO`, y coincide con el conjunto de intervalos retenidos.
- `contig_ploidy.tsv`, `global_read_depth.tsv`: ploidía, `PLOIDY_GQ`, `GLOBAL_READ_DEPTH` y `AVERAGE_PLOIDY`.
- `log_q_c_tc.tsv`, `baseline_copy_number_t.tsv`: posteriores de copy number y baseline por muestra e intervalo.
- `mu_psi_s_log__.tsv`, `std_psi_s_log__.tsv`: variabilidad no explicada específica de muestra en escala log nativa.
- `caller/cohort-model/mu_psi_t_log__.tsv`: variabilidad no explicada asociada al intervalo en escala log nativa; `exp(log ψ)` se expone aparte como valor derivado cuando su fila coincide con un intervalo retenido.
- `mu_z_su.tsv`: factores de sesgo latentes por muestra.
- `warm_up_elbo_history.tsv`, `main_elbo_history.tsv`: ELBO e iteraciones guardadas cuando existen.
- Configuración JSON y `gcnvkernel_version.json` del modelo.

Los diagnósticos derivados se guardan junto a outputs en `diagnostics_cache/`, como resumen JSON, intervalos JSONL comprimidos y matrices NumPy. Una firma de los ficheros fuente invalida la caché ante cambios. Los endpoints sirven filas paginadas y solo leen el detalle de una muestra o intervalo cuando se solicita.

## Definiciones

- **CPM**: recuentos de un intervalo divididos por el total de recuentos de esa muestra y multiplicados por 1.000.000. Para dispersión entre muestras se aplica `log2(CPM + 1)`. Así, la profundidad total de secuenciación no se compara directamente como si fuera cobertura local.
- **Median-ratio**: para cada intervalo se calcula la media geométrica de recuentos positivos; para cada muestra, la mediana de las razones recuento/media geométrica. Los factores se reescalan para que su media geométrica sea 1. Se ofrece como normalización alternativa y puede quedar no disponible si los datos no permiten estimarla.
- **MAD de cobertura**: mediana de `abs(log2(normalizado + 1) - mediana(log2(normalizado + 1)))` entre muestras. SD, IQR y CV se guardan como métricas derivadas complementarias. La tabla y Manhattan indican explícitamente la normalización elegida.
- **Coverage/base**: `CollectReadCounts / (END - START + 1)`, con coordenadas `interval_list` 1-based inclusive. El interval list original guardado en el snapshot se valida contra coordenadas y orden de los HDF5; si discrepa, el análisis se detiene para evitar uniones incorrectas.
- **Cobertura normalizada para Interval Diagnosis**: factor de cada muestra = mediana de coverage/base del conjunto configurable (todos los intervalos originales o solo retenidos). La cobertura normalizada es coverage/base dividido por ese factor. Este cálculo exploratorio se separa de la normalización interna de gCNV.
- **Median normalized coverage**: mediana entre muestras válidas por intervalo. **Relative median coverage** divide esa mediana por la mediana de los valores por intervalo. **MAD normalized coverage** es la mediana de desviaciones absolutas alrededor de la mediana. **Robust CV (%)** = `1.4826 × MAD / mediana × 100`; se omite con denominador cero. **Zero coverage (%)** excluye muestras con dato no finito del denominador.
- **Denoised metrics**: el parser solo usa valores con la columna explícita `LOG2_COPY_RATIO` y matching exacto de coordenadas. La mediana y MAD se conservan en esa escala; `robust sigma log2 = 1.4826 × MAD`. La app no transforma ratios cuya escala no esté declarada.
- **Fracción de ceros / bajos**: proporción de muestras con recuento exactamente 0 o menor al umbral `low_count_filter_count_threshold` del workflow. Son derivados de los HDF5.
- **Entropía posterior**: normaliza los log posteriores `COPY_NUMBER_k` con log-sum-exp y calcula `-Σ p log(p) / log(K)`, con `K` estados disponibles en esa tabla. Un valor alto expresa incertidumbre posterior, no probabilidad de que un CNV sea real.
- **Frecuencia MAP no baseline**: fracción entre muestras con posterior y baseline disponibles cuyo estado MAP difiere del `baseline_copy_number_t`. Es una señal de estado modelado y no una tasa clínica de eventos.
- **Varianza no explicada por intervalo**: valor nativo `mu_psi_t_log__` indexado por orden exacto del interval list retenido; `exp(log ψ)` es una transformación derivada. No se llama residual de cobertura.
- **Z robusto de muestra**: mediana/MAD escalada por 1.4826 aplicada a log(ψ) nativo. El umbral inicial z ≥ 3 es una guía exploratoria configurable en la interfaz; nunca elimina muestras.
- **Outlier exploratorio de intervalo**: z robusto ≥ 3 sobre MAD de CPM logarítmico calculado entre intervalos retenidos; nunca elimina intervalos.
- **ELBO**: puntos e iteraciones leídos de los ficheros tracking; las épocas, duración y convergencia no se inventan si el output no los reporta explícitamente.

Los valores nativos se distinguen en la interfaz de las transformaciones derivadas. Los valores ausentes permanecen ausentes. GATK no publica en los outputs inspeccionados un motivo individual por intervalo excluido: se presenta la pertenencia a FilterIntervals y el motivo específico como desconocido/no disponible, sin reconstrucción especulativa. Segmental duplication, mappability y anotaciones de GC solo se muestran si existen tracks/outputs compatibles. Los defaults de GATK se indican como no consultados cuando no se pueden recuperar de una fuente de esa versión.

## Escala y limitaciones

La tabla de intervalos usa páginas de hasta 200 filas; el tamaño inicial es 50. Los heatmaps de muestras usan orden jerárquico average-linkage para GQ y loadings, con valores ausentes imputados con mediana de columna solo para calcular el orden (las celdas siguen apareciendo como NA). El heatmap de entropía agrega intervalos retenidos por bins genómicos de 1 Mb (media, mediana o Q90) y muestra como máximo 200 bins y 100 muestras; el heatmap de estados MAP conserva intervalos exactos porque promediar estados categóricos no es válido. Las etiquetas se limitan para evitar saturación. El detalle HDF5/TSV es bajo demanda. CSV permite exportar el conjunto filtrado. La caché evita recalcular métricas al cambiar filtros.

Las matrices se mantienen en memoria durante el primer cálculo de caché (100 × 5.000 equivale a unos pocos MB para recuentos float32, además de matrices temporales). El parser requiere que cada muestra comparta exactamente la misma definición y orden de intervalos. Las curvas ELBO disponibles no bastan por sí mismas para certificar convergencia o idoneidad clínica.
