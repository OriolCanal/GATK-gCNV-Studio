document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('table.sortable-filterable-table').forEach((table) => {
    const head = table.tHead;
    const body = table.tBodies[0];
    const headerRow = head?.rows[0];
    if (!head || !body || !headerRow) return;
    const columns = [...headerRow.cells].map((cell, index) => ({
      index,
      label: cell.textContent.trim(),
      direction: 0,
      numeric: /[%]|cobertura|fold|correlation|outlier|score/i.test(cell.textContent),
    }));
    columns.forEach((column) => {
      const header = headerRow.cells[column.index];
      const label = column.label || `Columna ${column.index + 1}`;
      const sortButton = document.createElement('button');
      sortButton.type = 'button';
      sortButton.className = 'table-sort-button';
      sortButton.textContent = label;
      sortButton.setAttribute('aria-label', `Ordenar por ${label}`);
      sortButton.addEventListener('click', () => {
        column.direction = column.direction === 1 ? -1 : 1;
        columns.forEach((other) => {
          const button = headerRow.cells[other.index].querySelector('.table-sort-button');
          if (other !== column) other.direction = 0;
          if (button) button.dataset.direction = other.direction === 1 ? 'asc' : other.direction === -1 ? 'desc' : '';
        });
        const rows = [...body.rows].filter((row) => !row.classList.contains('table-filter-row'));
        rows.sort((left, right) => {
          const aCell = left.cells[column.index], bCell = right.cells[column.index];
          const aCheck = aCell?.querySelector('input[type="checkbox"]');
          const bCheck = bCell?.querySelector('input[type="checkbox"]');
          let order = 0;
          if (aCheck && bCheck) order = Number(aCheck.checked) - Number(bCheck.checked);
          else {
            const a = aCell?.innerText.trim() || '', b = bCell?.innerText.trim() || '';
            const numericValue = (value) => {
              const normalized = value.trim().replace(/\s/g, '').replace('%', '').replace(',', '.');
              return /^[-+]?(?:\d+\.?\d*|\.\d+)$/.test(normalized) ? Number(normalized) : NaN;
            };
            const aNum = numericValue(a), bNum = numericValue(b);
            order = Number.isFinite(aNum) && Number.isFinite(bNum)
              ? aNum - bNum : a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
          }
          return order * column.direction;
        });
        rows.forEach((row) => body.appendChild(row));
      });
      header.replaceChildren(sortButton);
    });

    const toolbar = document.createElement('section');
    toolbar.className = 'table-filter-toolbar';
    const toolbarTitle = document.createElement('strong');
    toolbarTitle.textContent = 'Filtros';
    const rules = document.createElement('div');
    rules.className = 'table-filter-rules';
    const addButton = document.createElement('button');
    addButton.type = 'button';
    addButton.className = 'table-add-filter';
    addButton.textContent = '+ Añadir filtro';
    const tableScroll = table.closest('.table-scroll');
    if (tableScroll?.parentElement) tableScroll.parentElement.insertBefore(toolbar, tableScroll);
    toolbar.append(toolbarTitle, rules, addButton);
    const activeFilters = [];
    addButton.addEventListener('click', () => addFilter());

    function addFilter() {
      const rule = document.createElement('div');
      rule.className = 'table-filter-rule';
      const columnSelect = document.createElement('select');
      columnSelect.setAttribute('aria-label', 'Columna a filtrar');
      columns.forEach((column) => {
        const option = document.createElement('option');
        option.value = String(column.index);
        option.textContent = column.label || `Columna ${column.index + 1}`;
        columnSelect.appendChild(option);
      });
      const operatorSelect = document.createElement('select');
      operatorSelect.setAttribute('aria-label', 'Operador del filtro');
      const valueWrap = document.createElement('span');
      valueWrap.className = 'table-filter-value';
      const removeButton = document.createElement('button');
      removeButton.type = 'button';
      removeButton.className = 'table-remove-filter';
      removeButton.textContent = 'Quitar';
      removeButton.setAttribute('aria-label', 'Quitar filtro');
      const filter = { rule, columnSelect, operatorSelect, valueWrap, input: null };

      const rebuildRule = () => {
        const column = columns[Number(columnSelect.value)];
        const sampleInclude = table.classList.contains('qc-sample-table') && column.index === 0;
        const operators = column.numeric
          ? [['>', 'Mayor que'], ['<', 'Menor que'], ['>=', 'Mayor o igual'], ['<=', 'Menor o igual'], ['=', 'Igual a']]
          : sampleInclude
            ? [['', 'Todas'], ['included', 'Incluida'], ['excluded', 'Excluida']]
            : [['contains', 'Contiene'], ['equals', 'Es igual a'], ['not_contains', 'No contiene']];
        operatorSelect.replaceChildren(...operators.map(([value, label]) => {
          const option = document.createElement('option');
          option.value = value;
          option.textContent = label;
          return option;
        }));
        valueWrap.replaceChildren();
        filter.input = null;
        if (!sampleInclude) {
          const input = document.createElement('input');
          input.type = column.numeric ? 'number' : 'search';
          if (column.numeric) input.step = 'any';
          input.placeholder = column.numeric ? 'Valor' : 'Texto';
          input.setAttribute('aria-label', `Valor para ${column.label}`);
          input.addEventListener('input', applyFilters);
          valueWrap.appendChild(input);
          filter.input = input;
        }
        applyFilters();
      };
      columnSelect.addEventListener('change', rebuildRule);
      operatorSelect.addEventListener('change', applyFilters);
      removeButton.addEventListener('click', () => {
        activeFilters.splice(activeFilters.indexOf(filter), 1);
        rule.remove();
        applyFilters();
      });
      rule.append(columnSelect, operatorSelect, valueWrap, removeButton);
      rules.appendChild(rule);
      activeFilters.push(filter);
      rebuildRule();
    }

    function applyFilters() {
      [...body.rows].forEach((row) => {
        const matches = activeFilters.every((filter) => {
          const column = columns[Number(filter.columnSelect.value)];
          const cell = row.cells[column.index];
          const operator = filter.operatorSelect.value;
          if (!column.numeric && table.classList.contains('qc-sample-table') && column.index === 0) {
            if (!operator) return true;
            const checked = Boolean(cell?.querySelector('input[type="checkbox"]')?.checked);
            return operator === 'included' ? checked : !checked;
          }
          const raw = filter.input?.value || '';
          if (!raw) return true;
          if (column.numeric) {
            const expected = Number(raw);
            const actual = Number((cell?.innerText || '').trim().replace('%', '').replace(',', '.'));
            if (!Number.isFinite(actual)) return false;
            if (operator === '>') return actual > expected;
            if (operator === '<') return actual < expected;
            if (operator === '>=') return actual >= expected;
            if (operator === '<=') return actual <= expected;
            return actual === expected;
          }
          const actual = (cell?.innerText || '').trim().toLocaleLowerCase();
          const expected = raw.toLocaleLowerCase();
          if (operator === 'equals') return actual === expected;
          if (operator === 'not_contains') return !actual.includes(expected);
          return actual.includes(expected);
        });
        row.hidden = !matches;
      });
    }
  });

  document.querySelectorAll('.model-run-item[data-model-status-url]').forEach((row) => {
    const chip = row.querySelector('.model-run-status');
    const deleteForm = row.querySelector('.delete-model-form');
    const error = row.querySelector('.model-run-error');
    const labels = {
      queued: 'En cola', running: 'En ejecución', cancel_requested: 'Cancelando',
      succeeded: 'Finalizado', succeeded_with_parse_warnings: 'Finalizado con avisos',
      failed: 'Fallido', cancelled: 'Cancelado'
    };
    let active = Boolean(chip && ['queued', 'running', 'cancel_requested'].some((status) => chip.classList.contains(status)));
    if (!chip || !active) return;
    const refreshStatus = async () => {
      try {
        const response = await fetch(row.dataset.modelStatusUrl, { headers: { Accept: 'application/json' }, cache: 'no-store' });
        if (!response.ok) return;
        const data = await response.json();
        chip.textContent = labels[data.status] || data.status.replaceAll('_', ' ');
        chip.className = `status-chip model-run-status ${data.status}`;
        if (data.error_message && error) { error.textContent = data.error_message.slice(0, 220); error.hidden = false; }
        if (deleteForm) deleteForm.hidden = data.status !== 'failed';
        active = ['queued', 'running', 'cancel_requested'].includes(data.status);
        if (!active) window.clearInterval(timer);
      } catch (error) { console.warn('No se pudo actualizar el estado del modelo.', error); }
    };
    const timer = window.setInterval(refreshStatus, 3000);
    refreshStatus();
  });

  const qualityNode = document.getElementById('model-quality-data');
  if (qualityNode) {
    try { renderModelQuality(JSON.parse(qualityNode.textContent)); }
    catch (error) { console.error('No se pudieron representar las métricas del modelo.', error); }
  }

  document.querySelectorAll('.model-quality-tabs').forEach((tabs) => {
    const buttons = [...tabs.querySelectorAll('[data-quality-tab]')];
    const panels = [...document.querySelectorAll('[data-quality-panel]')];
    const activate = (name) => {
      buttons.forEach((button) => {
        const selected = button.dataset.qualityTab === name;
        button.classList.toggle('active', selected);
        button.setAttribute('aria-selected', String(selected));
      });
      panels.forEach((panel) => { panel.hidden = panel.dataset.qualityPanel !== name; });
    };
    buttons.forEach((button) => button.addEventListener('click', () => activate(button.dataset.qualityTab)));
    activate(buttons[0]?.dataset.qualityTab || 'overview');
  });

  const storageMode = document.getElementById('bam-storage-mode');
  const updateStorageMode = () => {
    const symlink = storageMode?.value === 'symlink';
    const copyFields = document.getElementById('bam-copy-fields');
    const symlinkFields = document.getElementById('bam-symlink-fields');
    if (copyFields) copyFields.hidden = symlink;
    if (symlinkFields) symlinkFields.hidden = !symlink;
    copyFields?.querySelectorAll('input').forEach((input) => { input.disabled = symlink; input.required = !symlink && input.name === 'alignment_files'; });
    symlinkFields?.querySelectorAll('textarea').forEach((input) => { input.disabled = !symlink; input.required = symlink; });
  };
  storageMode?.addEventListener('change', updateStorageMode);
  updateStorageMode();

  const bamForm = document.getElementById('bam-batch-form');
  bamForm?.addEventListener('submit', (event) => {
    if (storageMode?.value !== 'copy') return;
    event.preventDefault();
    transferBamBatch(bamForm);
  });

  const validationBamForm = document.getElementById('validation-bam-form');
  validationBamForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    transferBamBatch(validationBamForm);
  });

  const qcNode = document.getElementById('project-qc-data');
  if (qcNode) {
    try {
      const qc = JSON.parse(qcNode.textContent);
      let visibleQc = qc;
      const updateSelectedQcCharts = () => {
        const selectedNames = new Set([...document.querySelectorAll('.qc-sample-table tbody tr')]
          .filter((row) => row.querySelector('input[name="included_sample_ids"]')?.checked)
          .map((row) => row.cells[1]?.textContent.trim()).filter(Boolean));
        const selectedSamples = qc.samples.filter((sample) => selectedNames.has(sample.sample_id));
        const correlationLabels = qc.correlation.labels.filter((label) => selectedNames.has(label));
        const correlationIndexes = correlationLabels.map((label) => qc.correlation.labels.indexOf(label));
        const pcaPoints = qc.pca.points.filter((point) => selectedNames.has(point.sample_id));
        visibleQc = {
          ...qc,
          samples: selectedSamples,
          correlation: {
            ...qc.correlation,
            labels: correlationLabels,
            matrix: correlationIndexes.map((row) => correlationIndexes.map((column) => qc.correlation.matrix[row][column])),
          },
          cluster_order: qc.cluster_order.filter((label) => selectedNames.has(label)),
          pca: { ...qc.pca, points: pcaPoints },
        };
        renderQcMetricDistributions(selectedSamples);
        renderQcCorrelation(visibleQc.correlation, visibleQc.cluster_order);
        renderQcPca(visibleQc.pca);
      };
      document.querySelectorAll('.qc-sample-table input[name="included_sample_ids"]')
        .forEach((checkbox) => checkbox.addEventListener('change', updateSelectedQcCharts));
      updateSelectedQcCharts();
      setupQcDownloads(() => visibleQc, qcNode.dataset.projectName || 'cohorte');
    } catch (error) {
      console.error('No se pudieron representar las métricas de QC.', error);
    }
  }

  const input = document.querySelector('#bed_file');
  const name = document.querySelector('#file-name');
  if (input && name) {
    input.addEventListener('change', () => {
      name.textContent = input.files.length ? input.files[0].name : 'Arrastra o selecciona un BED';
    });
    const drop = input.closest('.file-drop');
    drop?.addEventListener('dragover', (event) => { event.preventDefault(); drop.classList.add('drag-active'); });
    drop?.addEventListener('dragleave', () => drop.classList.remove('drag-active'));
    drop?.addEventListener('drop', (event) => {
      event.preventDefault(); drop.classList.remove('drag-active');
      if (event.dataTransfer.files.length) {
        input.files = event.dataTransfer.files;
        input.dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
  }

  const reopenJobLog = sessionStorage.getItem('reopenJobLog');
  if (reopenJobLog) {
    const entry = document.querySelector(`.job-entry[data-job-id="${reopenJobLog}"]`);
    const details = entry?.querySelector('.live-log');
    if (details) { details.open = true; entry.scrollIntoView({ block: 'center' }); }
    sessionStorage.removeItem('reopenJobLog');
  }

  document.querySelectorAll('.job-entry[data-status-url]').forEach((entry) => {
    const status = entry.querySelector('.job-status');
    const output = entry.querySelector('.live-log-output');
    const liveLabel = entry.querySelector('.log-live-label');
    const logDetails = entry.querySelector('.live-log');
    if (!status || !output) return;
    logDetails?.addEventListener('toggle', () => {
      if (logDetails.open) output.scrollTop = output.scrollHeight;
    });
    let wasActive = ['queued', 'running', 'cancel_requested'].includes(status.textContent.trim());
    let reloadedForCompletion = false;
    const poll = async () => {
      try {
        const statusResponse = await fetch(entry.dataset.statusUrl, { headers: { Accept: 'application/json' }, cache: 'no-store' });
        if (!statusResponse.ok) return;
        const statusData = await statusResponse.json();
        const previousStatus = status.textContent.trim();
        status.textContent = statusData.status;
        status.className = `status-chip ${statusData.status} job-status`;
        entry.querySelector('.job-dot').className = `job-dot ${statusData.status}`;
        if (liveLabel) liveLabel.textContent = statusData.status === 'running' ? 'EN DIRECTO' : statusData.status.toUpperCase();
        const logResponse = await fetch(`${entry.dataset.logUrl}?offset=${entry.dataset.logOffset || 0}`, { headers: { Accept: 'application/json' }, cache: 'no-store' });
        if (!logResponse.ok) return;
        const logData = await logResponse.json();
        if (logData.content) {
          if (output.textContent === 'Cargando log…') output.textContent = '';
          const followOutput = output.scrollHeight - output.scrollTop - output.clientHeight < 40;
          output.textContent += logData.content;
          if (logDetails.open && followOutput) output.scrollTop = output.scrollHeight;
        }
        entry.dataset.logOffset = String(logData.offset);
        const terminal = ['succeeded', 'failed', 'cancelled', 'succeeded_with_parse_warnings'].includes(statusData.status);
        if (previousStatus === 'queued' && statusData.status === 'running') {
          if (logDetails.open) sessionStorage.setItem('reopenJobLog', entry.dataset.jobId);
          window.location.reload();
          return;
        }
        if (terminal && Number(logData.offset) >= Number(logData.total_bytes) && wasActive && !reloadedForCompletion) {
          reloadedForCompletion = true;
          sessionStorage.setItem('reopenJobLog', entry.dataset.jobId);
          window.location.reload();
          return;
        }
        if (terminal && Number(logData.offset) >= Number(logData.total_bytes)) window.clearInterval(timer);
        if (previousStatus === 'running' || previousStatus === 'queued') wasActive = true;
      } catch (_) {
        // Polling is best-effort; the page and saved log remain available if the worker is offline.
      }
    };
    const timer = window.setInterval(poll, 1500);
    poll();
  });

  // The BAM page renders QC metrics from the database at request time. Refresh
  // that page after an active batch finishes so its table and cohort summary
  // include the outputs just parsed by the worker.
  document.querySelectorAll('.batch-card[data-qc-status-url]').forEach((batch) => {
    let refreshing = false;
    const pollQcStatus = async () => {
      try {
        const response = await fetch(batch.dataset.qcStatusUrl, {
          headers: { Accept: 'application/json' }, cache: 'no-store'
        });
        if (!response.ok) return;
        const result = await response.json();
        const terminal = ['succeeded', 'succeeded_with_parse_warnings', 'failed', 'cancelled'].includes(result.status);
        if (terminal && !refreshing) {
          refreshing = true;
          window.location.reload();
        }
      } catch (_) {
        // Keep polling; the worker or web server may be briefly unavailable.
      }
    };
    const timer = window.setInterval(() => {
      if (refreshing) window.clearInterval(timer);
      else pollQcStatus();
    }, 2500);
    pollQcStatus();
  });

  document.querySelectorAll('[data-cohort-qc-status-url]').forEach((cohort) => {
    let reloadStarted = false;
    const refreshCohortStatus = async () => {
      try {
        const response = await fetch(cohort.dataset.cohortQcStatusUrl, { cache: 'no-store' });
        if (!response.ok) return;
        const result = await response.json();
        if (['succeeded', 'succeeded_with_parse_warnings', 'failed', 'cancelled'].includes(result.status) && !reloadStarted) {
          reloadStarted = true;
          window.location.reload();
        }
      } catch (_) { /* Retain the current view if the server is briefly unavailable. */ }
    };
    window.setInterval(refreshCohortStatus, 2500);
    refreshCohortStatus();
  });

  const metricsNode = document.querySelector('#bed-metrics-data');
  if (metricsNode) {
    try {
      const metrics = JSON.parse(metricsNode.textContent);
      renderIntervalTable(metrics.gc?.intervals || []);
      renderHistogram('length-chart', metrics.original.interval_lengths, 40, 'bp');
      const lengths = metrics.original.interval_lengths || [];
      const lengthChart = document.querySelector('#length-chart')?.closest('.chart-card')?.querySelector('.chart-title small');
      if (lengths.length && lengthChart) {
        const bounds = lengths.reduce((result, value) => ({ min: Math.min(result.min, value), max: Math.max(result.max, value) }), { min: Number.POSITIVE_INFINITY, max: Number.NEGATIVE_INFINITY });
        lengthChart.textContent = `BED original · mínimo ${bounds.min.toLocaleString()} bp · máximo ${bounds.max.toLocaleString()} bp`;
      }
      renderHistogram('gc-histogram', metrics.gc.intervals.map((x) => x.gc).filter(Number.isFinite), 40, 'GC');
      renderChromosomes('chromosome-chart', metrics.original.intervals_by_chromosome);
      renderGenomeTrack('gc-chart', metrics.gc.intervals, 'gc', '#43865c');
      renderGenomeTrack('mappability-interval-chart', metrics.gc.intervals, 'mappability', '#568ba2');
      renderGenomeTrack('segmental-duplication-chart', metrics.gc.intervals, 'segmental_duplication', '#b97845');
      renderHistogram('mappability-chart', metrics.gc.intervals.map((x) => x.mappability).filter(Number.isFinite), 30, 'score');
      renderHistogram('segmental-duplication-histogram', metrics.gc.intervals.map((x) => x.segmental_duplication).filter(Number.isFinite), 30, 'score');
      renderScatter('gc-mappability-chart', metrics.gc.intervals);
    } catch (error) {
      console.error('No se pudieron representar las métricas BED.', error);
    }
  }
});

document.addEventListener('DOMContentLoaded', () => {
  const root = document.getElementById('cohort-dashboard');
  const dataNode = document.getElementById('cohort-diagnostics-data');
  if (!root || !dataNode) return;
  const data = JSON.parse(dataNode.textContent);
  const $ = (selector) => document.querySelector(selector);
  const sessionFilterKey=`cohort-diagnostics:${data.provenance?.model_run_id||'current'}`;
  try{const saved=JSON.parse(sessionStorage.getItem(sessionFilterKey)||'{}');Object.entries(saved).forEach(([id,value])=>{const el=document.getElementById(id);if(!el)return;if(el.multiple){[...el.options].forEach(option=>{option.selected=Array.isArray(value)&&value.includes(option.value);});}else el.value=value;});}catch(_error){}
  const ctx = (id, height = 250) => {
    const canvas = document.getElementById(id);
    if (!canvas) return null;
    const width = Math.max(canvas.clientWidth || 500, 320);
    const ratio = window.devicePixelRatio || 1;
    canvas.width = width * ratio; canvas.height = height * ratio;
    canvas.style.height = `${height}px`;
    const context = canvas.getContext('2d'); if (!context) return null; context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    return { canvas, context, width, height };
  };
  const color = (value, max) => {
    if (!Number.isFinite(value)) return '#e6e9e7';
    const t = Math.max(0, Math.min(1, value / (max || 1)));
    const stops = [[68,1,84],[59,82,139],[33,145,140],[94,201,98],[253,231,37]];
    const x = t * (stops.length - 1), i = Math.min(stops.length - 2, Math.floor(x)), f = x - i;
    const rgb = stops[i].map((v, k) => Math.round(v * (1-f) + stops[i+1][k] * f));
    return `rgb(${rgb.join(',')})`;
  };
  const drawHist = (id, bins, label = '') => {
    const view = ctx(id, 220); if (!view) return;
    const {context:c,width:w,height:h} = view, pad = 34;
    c.font = '10px sans-serif'; c.fillStyle = '#687784'; c.fillText(label, pad, 15);
    if (!bins?.length) { c.fillText('No disponible', pad, 50); return; }
    const max = Math.max(...bins.map(b => b.count), 1), bw = (w - pad - 10) / bins.length;
    bins.forEach((b,i) => { const bh=(h-55)*b.count/max; c.fillStyle='#39845c'; c.fillRect(pad+i*bw,h-30-bh,Math.max(1,bw-2),bh); });
    c.fillStyle='#718079'; c.fillText(`${bins[0].start.toPrecision(3)}`,pad,h-10); c.fillText(`${bins.at(-1).end.toPrecision(3)}`,w-55,h-10);
    c.save(); c.translate(11,h/2); c.rotate(-Math.PI/2); c.fillText('Muestras / intervalos',0,0); c.restore();
  };
  const drawBoxPlot = (id, values, logScale = false) => {
    const v=ctx(id,150);if(!v)return;let nums=values.filter(x=>Number.isFinite(x)&&x>0).map(x=>logScale?Math.log10(x):x).sort((a,b)=>a-b);if(!nums.length){v.context.fillStyle='#718079';v.context.fillText('No disponible',12,30);return;}const q=p=>nums[Math.min(nums.length-1,Math.floor((nums.length-1)*p))],lo=nums[0],hi=nums.at(-1),q1=q(.25),med=q(.5),q3=q(.75),pad=24,max=Math.max(hi,lo+1),x=n=>pad+(n-lo)/(max-lo||1)*(v.width-pad*2),y=75,c=v.context;c.strokeStyle='#597362';c.beginPath();c.moveTo(x(lo),y);c.lineTo(x(hi),y);c.moveTo(x(lo),y-8);c.lineTo(x(lo),y+8);c.moveTo(x(hi),y-8);c.lineTo(x(hi),y+8);c.stroke();c.fillStyle='#dcecdf';c.fillRect(x(q1),y-19,Math.max(1,x(q3)-x(q1)),38);c.strokeStyle='#33724b';c.strokeRect(x(q1),y-19,Math.max(1,x(q3)-x(q1)),38);c.beginPath();c.moveTo(x(med),y-19);c.lineTo(x(med),y+19);c.stroke();c.fillStyle='#627168';c.font='9px sans-serif';c.fillText(`Min ${fmt(lo)} · Q1 ${fmt(q1)} · Mediana ${fmt(med)} · Q3 ${fmt(q3)} · máx. ${fmt(hi)}${logScale?' (log₁₀)':''}`,pad,132);
  };
  const drawViolin = (id, values, logScale = false) => {
    const v=ctx(id,190);if(!v)return;const nums=values.filter(x=>Number.isFinite(x)&&x>0).map(x=>logScale?Math.log10(x):x);if(nums.length<2){v.context.fillStyle='#718079';v.context.fillText('Se requieren al menos dos muestras',12,30);return;}const mean=nums.reduce((a,b)=>a+b,0)/nums.length,sd=Math.sqrt(nums.reduce((a,b)=>a+(b-mean)**2,0)/nums.length)||1,band=Math.max(1.06*sd*Math.pow(nums.length,-.2),1e-6),lo=Math.min(...nums),hi=Math.max(...nums),grid=Array.from({length:48},(_,i)=>lo+(hi-lo)*i/47),dens=grid.map(x=>nums.reduce((sum,value)=>sum+Math.exp(-.5*((x-value)/band)**2),0)/(nums.length*band*Math.sqrt(2*Math.PI))),max=Math.max(...dens),pad=35,c=v.context,mid=v.width/2,span=v.height-42;c.fillStyle='#c9dfce';c.strokeStyle='#43865c';c.beginPath();grid.forEach((x,i)=>{const y=18+(x-lo)/(hi-lo||1)*span,w=90*dens[i]/max;i?c.lineTo(mid+w,y):c.moveTo(mid+w,y);});for(let i=grid.length-1;i>=0;i--){const x=grid[i],y=18+(x-lo)/(hi-lo||1)*span,w=90*dens[i]/max;c.lineTo(mid-w,y);}c.closePath();c.fill();c.stroke();const median=nums.slice().sort((a,b)=>a-b)[Math.floor(nums.length/2)],my=18+(median-lo)/(hi-lo||1)*span;c.strokeStyle='#a4493f';c.beginPath();c.moveTo(mid-90,my);c.lineTo(mid+90,my);c.stroke();c.fillStyle='#627168';c.font='9px sans-serif';c.fillText(`Distribución ψ ${logScale?'(log₁₀)':''} · n=${nums.length}`,pad,v.height-5);
  };
  const drawLine = (id, points) => {
    const v=ctx(id,210); if(!v)return; const {context:c,width:w,height:h}=v,p=32;
    if(!points?.length){c.fillStyle='#718079';c.fillText('Tracking no disponible',p,35);return;}
    const ys=points.map(x=>x.elbo).filter(Number.isFinite), lo=Math.min(...ys), hi=Math.max(...ys)||1;
    c.strokeStyle='#387b55';c.lineWidth=1.5;c.beginPath();
    points.forEach((point,i)=>{const x=p+i*(w-p-8)/Math.max(1,points.length-1),y=h-25-(point.elbo-lo)/(hi-lo||1)*(h-45);i?c.lineTo(x,y):c.moveTo(x,y);});c.stroke();
    c.fillStyle='#718079';c.font='10px sans-serif';c.fillText('Iteraciones guardadas',p,h-5);c.save();c.translate(11,h/2);c.rotate(-Math.PI/2);c.fillText('ELBO',0,0);c.restore();
  };
  const intervalTooltip = p => `${p.contig}:${p.start}-${p.end} · Gen: ${p.gene||'N/D'} · Isoforma: ${p.transcript_id||'N/D'} · Exón: ${p.exon_number||'N/D'}`;
  const drawHeatmap = (id, matrix, rows, cols, options={}) => {
    const canvas=document.getElementById(id); if(!canvas)return;
    const labelW=rows.length<=100?145:12, top=options.columnGroups?30:cols.length<=50?28:8;
    // Keep backing canvases within browser canvas limits. Large cohort heatmaps
    // otherwise allocate hundreds of MB and can make every chart on the page
    // stop rendering when the browser drops the 2D context.
    const requestedCellW=options.cellWidth||18, cellW=Math.max(1,Math.min(requestedCellW,(12000-labelW-12)/Math.max(cols.length,1)));
    const requestedCellH=options.cellHeight||10, cellH=Math.max(1,Math.min(requestedCellH,(6000-top-12)/Math.max(rows.length,1)));
    const width=Math.max(320,labelW+cols.length*cellW+12), height=Math.max(140,top+rows.length*cellH+12), ratio=options.pixelRatio||Math.min(window.devicePixelRatio||1,1);
    canvas.width=width*ratio;canvas.height=height*ratio;canvas.style.width=options.scroll?`${width}px`:`${Math.min(width,canvas.parentElement.clientWidth||width)}px`;canvas.style.height=`${height}px`;
    const c=canvas.getContext('2d');if(!c){canvas.title='El navegador no pudo crear el contexto gráfico para este heatmap.';return;}c.setTransform(ratio,0,0,ratio,0,0);c.clearRect(0,0,width,height);
    const vals=matrix.flat().filter(x=>Number.isFinite(x));const abs=vals.reduce((current,value)=>Math.max(current,Math.abs(value)),1);const max=options.max??vals.reduce((current,value)=>Math.max(current,value),1);
    cols.forEach((name,j)=>{if(cols.length<=50){c.save();c.translate(labelW+j*cellW+3,top-2);c.rotate(-.6);c.font='8px sans-serif';c.fillStyle='#65736a';c.fillText(name,0,0);c.restore();}});
    (options.columnGroups||[]).forEach(group=>{const startX=labelW+group.start*cellW,endX=labelW+group.end*cellW;c.fillStyle='#536159';c.font='9px sans-serif';c.textAlign='center';c.fillText(group.label,(startX+endX)/2,12);if(group.start>0){c.strokeStyle='#77867c';c.beginPath();c.moveTo(startX,top-2);c.lineTo(startX,height-4);c.stroke();}});c.textAlign='start';
    rows.forEach((name,i)=>{if(rows.length<=100){c.font='8px sans-serif';c.fillStyle='#536159';c.fillText(name.slice(0,22),2,top+i*cellH+cellH-1);}matrix[i]?.forEach((value,j)=>{if(options.categoryColors&&Number.isFinite(value)){const category=Math.trunc(value);c.fillStyle=options.categoryColors[category]||options.otherCategoryColor||'#242424';}else if(options.diverging&&Number.isFinite(value)){const t=Math.min(1,Math.abs(value)/(options.max||abs));c.fillStyle=value<0?`rgb(${Math.round(248-90*t)},${Math.round(248-125*t)},${Math.round(248-150*t)})`:`rgb(${Math.round(248-150*t)},${Math.round(248-100*t)},${Math.round(248-90*t)})`;}else c.fillStyle=Number.isFinite(value)?color(Number(value),max):'#e6e9e7';c.fillRect(labelW+j*cellW,top+i*cellH,cellW,cellH);});});
    canvas._heatmapInfo={matrix,rows,cols,cellW,cellH,labelW,top,width,height,title:options.title||'Valores medidos; gris = dato ausente.',intervals:options.intervals,onRowClick:options.onRowClick};
    if(!canvas.dataset.tooltipBound){canvas.dataset.tooltipBound='true';canvas.addEventListener('mousemove',event=>{const info=canvas._heatmapInfo;if(!info)return;const rect=canvas.getBoundingClientRect(),x=(event.clientX-rect.left)*info.width/rect.width,y=(event.clientY-rect.top)*info.height/rect.height,row=Math.floor((y-info.top)/info.cellH),col=Math.floor((x-info.labelW)/info.cellW);if(row>=0&&row<info.rows.length&&col>=0&&col<info.cols.length){const value=info.matrix[row]?.[col],sample=data.samples.find(item=>item.sample_id===info.rows[row]);const interval=info.intervals?.[col];canvas.title=`${interval?intervalTooltip(interval)+'\n':''}${info.rows[row]}${sample?.batch?' · '+sample.batch:''} × ${info.cols[col]}: ${Number.isFinite(value)?value:'No disponible'}\n${info.title}`;}else canvas.title=info.title;});canvas.addEventListener('click',event=>{const info=canvas._heatmapInfo;if(!info?.onRowClick)return;const rect=canvas.getBoundingClientRect(),y=(event.clientY-rect.top)*info.height/rect.height,row=Math.floor((y-info.top)/info.cellH);if(row>=0&&row<info.rows.length)info.onRowClick(row);});}
  };
  const activate = (requestedName) => {
    const name = requestedName === 'samples' || requestedName === 'performance' ? 'overview' : requestedName;
    document.querySelectorAll('[data-diagnostic-tab]').forEach(b=>{const on=b.dataset.diagnosticTab===name;b.classList.toggle('active',on);b.setAttribute('aria-selected',String(on));});
    document.querySelectorAll('[data-diagnostic-panel]').forEach(p=>{p.hidden=p.dataset.diagnosticPanel!==name;});
    if(name==='intervals') loadIntervals(); if(name==='overview' && $('#sample-table-body')) { renderSamples(); renderPerformance(); }
    if(name==='ploidy') { renderSampleMatrices(); renderPloidyQuality(); }
  };
  document.querySelectorAll('[data-diagnostic-tab]').forEach(b=>b.addEventListener('click',()=>activate(b.dataset.diagnosticTab)));
  document.querySelectorAll('[data-open-diagnostic-tab]').forEach(b=>b.addEventListener('click',()=>activate(b.dataset.openDiagnosticTab)));
  document.getElementById('model-run-select')?.addEventListener('change',event=>{if(event.target.value)window.location.href=event.target.value;});
  document.getElementById('compare-models-button')?.addEventListener('click',async()=>{const selected=[...(document.getElementById('compare-models-select')?.selectedOptions||[])].map(option=>option.value),body=document.getElementById('model-comparison-rows');if(selected.length<2||selected.length>4){body.innerHTML='<tr><td colspan="12">Selecciona entre dos y cuatro modelos.</td></tr>';return;}body.innerHTML='<tr><td colspan="12">Cargando comparativa…</td></tr>';const query=new URLSearchParams();selected.forEach(id=>query.append('model_id',id));try{const response=await fetch(`${root.dataset.compareUrl}?${query}`),result=await response.json();if(!response.ok)throw new Error(result.error||'No se pudo comparar');body.replaceChildren();result.models.forEach(model=>{const row=document.createElement('tr');row.innerHTML=`<td>${esc(model.name)}</td><td>${esc(model.status)}</td><td>${esc(model.gatk_version||'—')} / ${esc(model.gcnvkernel_version||'—')}</td><td>${fmt(model.sample_count)}</td><td title="${esc((model.sample_ids||[]).join(', '))}">${esc([...(model.sample_ids||[]),...(model.capture_kits||[]),...(model.protocols||[])].join(' · ')||'—')}</td><td>${fmt(model.retained_interval_count)}</td><td>${fmt(model.filtered_percentage)}</td><td>${fmt(model.median_global_read_depth)}</td><td>${fmt(model.median_unexplained_variance)}</td><td>${fmt(model.median_posterior_entropy)}</td><td>${fmt(model.max_copy_number)} / ${fmt(model.max_bias_factors)}</td><td>${fmt(model.p_active)} / ${fmt(model.p_alt)}</td>`;if(model.unavailable_reason)row.title=model.unavailable_reason;body.append(row);});}catch(error){body.innerHTML=`<tr><td colspan="12">${esc(error.message)}</td></tr>`;}});
  const parameterRows=document.getElementById('caller-parameter-rows');let parameterSortDirection=1;
  document.getElementById('caller-parameter-search')?.addEventListener('input',event=>{const query=event.target.value.toLowerCase();parameterRows?.querySelectorAll('tr').forEach(row=>{row.hidden=!row.textContent.toLowerCase().includes(query);});});
  document.querySelector('[data-parameter-sort="parameter"]')?.addEventListener('click',()=>{if(!parameterRows)return;parameterSortDirection*=-1;[...parameterRows.rows].sort((a,b)=>parameterSortDirection*a.cells[0].textContent.localeCompare(b.cells[0].textContent)).forEach(row=>parameterRows.append(row));});
  const s=data.summary,m=data.metadata;
  drawHist('overview-variance-chart',data.samples.map(x=>x.unexplained_variance_exp_log_psi_derived).filter(Number.isFinite).length?hist(data.samples.map(x=>x.unexplained_variance_exp_log_psi_derived)) : [],'Exp(log ψ), valor derivado');
  drawHist('overview-depth-chart',hist(data.samples.map(x=>x.global_read_depth)),'GLOBAL_READ_DEPTH');
  drawHist('overview-gq-chart',hist(data.samples.map(x=>x.median_ploidy_gq)),'Mediana PLOIDY_GQ por muestra');
  const fv=ctx('filter-summary-chart',200);if(fv){const c=fv.context, vals=[s.retained_interval_count,s.excluded_interval_count], labels=['Retenidos','Excluidos'], max=Math.max(...vals,1);labels.forEach((l,i)=>{const y=38+i*55,bw=(fv.width-130)*vals[i]/max;c.fillStyle=i?'#9ba6a1':'#3d8a5d';c.fillRect(80,y,bw,27);c.fillStyle='#536159';c.font='11px sans-serif';c.fillText(`${l} · ${vals[i]}`,8,y+18);});}
  function hist(values,bins=40){const v=values.filter(Number.isFinite);if(!v.length)return[];const lo=Math.min(...v),hi=Math.max(...v);if(lo===hi)return[{start:lo,end:hi,count:v.length}];const counts=Array(bins).fill(0),width=(hi-lo)/bins;v.forEach(x=>counts[Math.min(bins-1,Math.floor((x-lo)/width))]++);return counts.map((count,i)=>({start:lo+i*width,end:i===bins-1?hi:lo+(i+1)*width,count}));}
  drawHeatmap('bias-factor-heatmap',data.bias_factor_heatmap.values,data.bias_factor_heatmap.samples,data.bias_factor_heatmap.factors,{diverging:true,title:'mu_z_su; paleta divergente centrada en cero'});
  let intervalPage=1, intervalResponse=null, intervalTimer=null, intervalSort='contig', intervalDirection='asc';
  const intervalParams=()=>new URLSearchParams({page:String(intervalPage),page_size:'50',metric:'median_normalized_coverage',metric_filter:'median_normalized_coverage',normalization:'library_size',contig:$('#interval-contig')?.value||'',status:$('#interval-status')?.value||'',gene:$('#interval-gene')?.value||'',search:$('#interval-search')?.value||'',gc_min:$('#interval-gc-min')?.value||'',gc_max:$('#interval-gc-max')?.value||'',mappability_min:$('#interval-map-min')?.value||'',mappability_max:$('#interval-map-max')?.value||'',median_coverage_min:$('#interval-coverage-min')?.value||'',median_coverage_max:$('#interval-coverage-max')?.value||'',coverage_variability_min:$('#interval-variability-min')?.value||'',coverage_variability_max:$('#interval-variability-max')?.value||'',sort:intervalSort,direction:intervalDirection});
  async function loadIntervals(){const params=intervalParams();try{const plotUrl=root.dataset.intervalsUrl.replace(/\/intervals$/,'/interval-plot'),medianParams=new URLSearchParams(params),cvParams=new URLSearchParams(params);medianParams.set('metric','median_normalized_coverage');cvParams.set('metric','robust_cv_percent');const [table,medianPlot,cvPlot]=await Promise.all([fetch(`${root.dataset.intervalsUrl}?${params}`),fetch(`${plotUrl}?${medianParams}`),fetch(`${plotUrl}?${cvParams}`)]);intervalResponse=await table.json();const medianData=await medianPlot.json(),cvData=await cvPlot.json();renderIntervalTable(intervalResponse);drawManhattan(medianData.points,'interval-median-coverage',1);drawManhattan(cvData.points,'interval-coverage-variability');renderDepthMad();}catch(e){console.error(e);}}
  function renderIntervalTable(result){const body=$('#interval-table-body');if(!body)return;$('#interval-result-count').textContent=`· ${result.total} intervalos`;$('#interval-page-label').textContent=`Página ${result.page} / ${result.pages}`;$('#interval-prev').disabled=result.page<=1;$('#interval-next').disabled=result.page>=result.pages;body.replaceChildren();result.rows.forEach(row=>{const tr=document.createElement('tr');tr.dataset.intervalIndex=row.interval_index;tr.innerHTML=`<td>${esc(row.contig)}</td><td>${row.start}</td><td>${row.end}</td><td title="${esc(row.gene||'Gen no anotado')}">${esc(row.gene||'N/D')}</td><td title="${esc(row.transcript_id||'Transcript no anotado')}">${esc(row.transcript_id||'N/D')}</td><td>${esc(row.exon_number||'N/D')}</td><td><span class="interval-state ${row.retained?'retained':'filtered'}">${row.retained?'Retenido':'Excluido'}</span></td><td>${fmt(row.mappability)}</td><td>${fmt(row.gc_content)}</td><td>${fmt(row.median_normalized_coverage)}</td><td>${fmt(row.robust_cv_percent)}</td>`;tr.tabIndex=0;tr.addEventListener('click',()=>showInterval(row.interval_index));body.append(tr);});}
  function drawManhattan(points,targetId,referenceLine=null){
    const view=ctx(targetId,300);if(!view)return;
    const {context:c,width:w,height:h}=view,pad=42,usable=w-pad-12,values=points.map(point=>point.value).filter(Number.isFinite),max=values.reduce((current,value)=>Math.max(current,value),Math.max(referenceLine||0,1));
    const chromKey=contig=>{const name=String(contig).replace(/^chr/i,'').toLowerCase(),numeric=Number(name);return Number.isFinite(numeric)?[0,numeric,'']:[1,({x:23,y:24,mt:25,m:25})[name]||99,name];};
    const contigs=[...new Set(points.map(point=>point.contig))].sort((a,b)=>{const left=chromKey(a),right=chromKey(b);return left[0]-right[0]||left[1]-right[1]||String(left[2]).localeCompare(String(right[2]));});
    const groups=contigs.map(contig=>points.filter(point=>point.contig===contig).sort((a,b)=>a.start-b.start||a.end-b.end));
    let offset=0;view.canvas._manhattanPoints=[];
    groups.forEach((group,chromosomeIndex)=>{
      const span=usable*group.length/Math.max(points.length,1),slot=span/Math.max(group.length,1);
      group.forEach((point,index)=>{
        if(!Number.isFinite(point.value))return;
        const x=pad+offset+(index+.5)*slot,y=h-32-point.value/max*(h-58),retainedColors=['#176b3a','#368f54'],excludedColors=['#a34d38','#c8785d'];
        c.fillStyle=point.retained?retainedColors[chromosomeIndex%2]:excludedColors[chromosomeIndex%2];c.strokeStyle='#fff';c.lineWidth=.8;c.beginPath();c.arc(x,y,3.1,0,Math.PI*2);c.fill();c.stroke();view.canvas._manhattanPoints.push({x,y,point});
      });
      c.fillStyle='#58675e';c.font='8px sans-serif';c.textAlign='center';c.fillText(contigs[chromosomeIndex],pad+offset+span/2,h-15);c.textAlign='start';
      offset+=span;if(chromosomeIndex<groups.length-1){c.strokeStyle='#d8dfda';c.beginPath();c.moveTo(pad+offset,h-32);c.lineTo(pad+offset,h-27);c.stroke();}
    });
    c.strokeStyle='#aeb9b1';c.beginPath();c.moveTo(pad,12);c.lineTo(pad,h-32);c.lineTo(w-6,h-32);c.stroke();
    for(let tick=0;tick<=4;tick++){const value=max*tick/4,y=h-32-value/max*(h-58);c.fillStyle='#647168';c.font='8px sans-serif';c.fillText(value.toPrecision(2),3,y+3);if(tick>0&&tick<4){c.strokeStyle='#edf0ed';c.beginPath();c.moveTo(pad,y);c.lineTo(w-6,y);c.stroke();}}
    if(referenceLine!==null){const y=h-32-referenceLine/max*(h-58);c.strokeStyle='#65756b';c.setLineDash([4,3]);c.beginPath();c.moveTo(pad,y);c.lineTo(w-6,y);c.stroke();c.setLineDash([]);}
    c.fillStyle='#536159';c.font='10px sans-serif';c.fillText(targetId==='interval-median-coverage'?'Median normalized coverage':'Coverage variability · robust CV (%)',4,10);c.font='9px sans-serif';c.fillText('Intervalos equidistantes, ordenados por cromosoma y posición',pad+8,h-1);
    view.canvas.title='Cada punto corresponde a un intervalo, distribuido equidistantemente en orden genómico. Verdes = retenidos; tonos cálidos = excluidos. Pasa el cursor o haz clic para inspeccionarlo.';
  }
  function nearestManhattanPoint(event){const canvas=event.currentTarget,rect=canvas.getBoundingClientRect(),x=(event.clientX-rect.left)*canvas.width/rect.width/(window.devicePixelRatio||1),y=(event.clientY-rect.top)*canvas.height/rect.height/(window.devicePixelRatio||1);let closest=null,distance=100;(canvas._manhattanPoints||[]).forEach(item=>{const d=Math.hypot(x-item.x,y-item.y);if(d<distance){closest=item;distance=d;}});return closest;}
  ['interval-median-coverage','interval-coverage-variability'].forEach(id=>{const canvas=document.getElementById(id);canvas?.addEventListener('click',event=>{const closest=nearestManhattanPoint(event);if(closest)showInterval(closest.point.interval_index);});canvas?.addEventListener('mousemove',event=>{const point=nearestManhattanPoint(event)?.point;canvas.title=point?`${intervalTooltip(point)} · ${point.interval_id||''} · ${point.retained?'RETAINED':'FILTERED'} · ${fmt(point.value)} · GC ${fmt(point.gc_content)} · mappability ${fmt(point.mappability)}`:'Cada punto es un intervalo; clic para inspeccionarlo.';});});
  async function showInterval(index){const area=$('#interval-detail');area.textContent='Cargando…';const url=new URL(root.dataset.intervalDetailUrl.replace(/\/0$/,`/${index}`),window.location.href);try{const d=await(await fetch(url)).json();const i=d.interval,measurements=d.sample_measurements||[];document.querySelectorAll('#interval-table-body tr').forEach(row=>row.classList.toggle('selected',Number(row.dataset.intervalIndex)===index));area.innerHTML=`<h3>${esc(i.interval_id||`${i.contig}:${i.start}-${i.end}`)}</h3><p>${i.retained?'RETAINED':'FILTERED'} · ${esc(i.primary_filter_reason||'sin motivo específico disponible')} · longitud ${i.length} bp</p><p>Gen ${esc(i.gene||'N/D')} · Transcript ID ${esc(i.transcript_id||'N/D')} · Exón ${esc(i.exon_number||'N/D')}</p><p>GC ${fmt(i.gc_content)} · mappability ${fmt(i.mappability)} · segmental duplication ${fmt(i.segmental_duplication_content)}</p><p>Median normalized coverage ${fmt(i.median_normalized_coverage)} · Relative ${fmt(i.relative_median_coverage)} · Robust CV ${fmt(i.robust_cv_percent)}% · Zero coverage ${fmt(i.zero_coverage_percentage)}%</p><p>Denoised median ${fmt(i.median_denoised_copy_ratio_log2)} log₂ · Robust sigma ${fmt(i.robust_sigma_log2)} log₂ · posterior samples ${fmt(i.denoised_sample_count)}</p><div class="diagnostic-grid-two"><article><h3>Cobertura cruda por base</h3><canvas class="diagnostic-chart" id="interval-raw-detail"></canvas></article><article><h3>Cobertura normalizada</h3><canvas class="diagnostic-chart" id="interval-normalized-detail"></canvas></article><article><h3>Denoised copy ratio · escala log₂ declarada</h3><canvas class="diagnostic-chart" id="interval-denoised-detail"></canvas></article></div><p class="muted-copy">Muestras mostradas: ${measurements.length} de ${fmt(d.sample_measurements_total)}; ordenadas por desviación absoluta de cobertura normalizada respecto a la mediana para facilitar la inspección de valores responsables de variabilidad. ${d.sample_measurements_truncated?'La tabla está limitada a las 50 muestras más desviadas.':''}</p><div class="table-scroll"><table><thead><tr><th>Muestra</th><th>Batch</th><th>Read count</th><th>Coverage/base</th><th>Normalized coverage</th><th>Denoised log₂ ratio</th><th>Posterior</th></tr></thead><tbody>${measurements.map(x=>`<tr><td>${esc(x.sample_id)}</td><td>${esc(x.batch||'—')}</td><td>${fmt(x.count_raw_observed)}</td><td>${fmt(x.coverage_per_base)}</td><td>${fmt(x.normalized_coverage)}</td><td>${fmt(x.denoised_copy_ratio_log2)}</td><td>${x.posterior?.states?.map(s=>`${s.copy_number}: ${s.probability.toFixed(3)}`).join(' · ')||'—'}</td></tr>`).join('')}</tbody></table></div><small>${esc(d.calculation_note)}</small>`;drawHist('interval-raw-detail',hist(measurements.map(x=>x.coverage_per_base).filter(Number.isFinite)),'Cobertura por base');drawHist('interval-normalized-detail',hist(measurements.map(x=>x.normalized_coverage).filter(Number.isFinite)),'Cobertura normalizada');drawHist('interval-denoised-detail',hist(measurements.map(x=>x.denoised_copy_ratio_log2).filter(Number.isFinite)),'Denoised log₂ copy ratio');}catch(e){area.textContent='No se pudo cargar el detalle: '+e.message;}}
  function renderDepthMad(){
    const view=ctx('interval-depth-mad',300);if(!view)return;
    const getNumber=id=>Number.parseFloat($('#'+id)?.value),status=$('#interval-status')?.value||'',contig=$('#interval-contig')?.value||'',gene=($('#interval-gene')?.value||'').toLowerCase(),search=($('#interval-search')?.value||'').toLowerCase();
    const gcMin=getNumber('interval-gc-min'),gcMax=getNumber('interval-gc-max'),mapMin=getNumber('interval-map-min'),mapMax=getNumber('interval-map-max'),covMin=getNumber('interval-coverage-min'),covMax=getNumber('interval-coverage-max'),varMin=getNumber('interval-variability-min'),varMax=getNumber('interval-variability-max');
    const within=(value,low,high)=>Number.isNaN(low)||value!==null&&value>=low ? (Number.isNaN(high)||value!==null&&value<=high) : false;
    const points=(data.interval_depth_mad||[]).filter(p=>(!status||(status==='retained')===p.retained)&&(!contig||p.contig===contig)&&(!gene||String(p.gene||'').toLowerCase().includes(gene))&&(!search||`${p.contig}:${p.start}-${p.end} ${p.gene||''} ${p.transcript_id||''} ${p.exon_number||''}`.toLowerCase().includes(search))&&within(p.gc_content,gcMin,gcMax)&&within(p.mappability,mapMin,mapMax)&&within(p.median_coverage,covMin,covMax)&&within(p.variability,varMin,varMax)&&Number.isFinite(p.mean_depth)&&Number.isFinite(p.mad));
    const {context:c,width:w,height:h}=view,pad={left:54,right:12,top:18,bottom:36},xMax=Math.max(...points.map(p=>p.mean_depth),1),yMax=Math.max(...points.map(p=>p.mad),1),plotW=w-pad.left-pad.right,plotH=h-pad.top-pad.bottom,px=x=>pad.left+x/xMax*plotW,py=y=>h-pad.bottom-y/yMax*plotH;
    c.strokeStyle='#bfc9c2';c.beginPath();c.moveTo(pad.left,pad.top);c.lineTo(pad.left,h-pad.bottom);c.lineTo(w-pad.right,h-pad.bottom);c.stroke();c.font='8px sans-serif';
    for(let tick=0;tick<=4;tick++){const fraction=tick/4,xValue=xMax*fraction,yValue=yMax*fraction,x=px(xValue),y=py(yValue);c.strokeStyle='#edf0ed';c.beginPath();c.moveTo(x,pad.top);c.lineTo(x,h-pad.bottom);c.moveTo(pad.left,y);c.lineTo(w-pad.right,y);c.stroke();c.fillStyle='#68766e';c.fillText(fmt(xValue),x-10,h-pad.bottom+12);c.fillText(fmt(yValue),3,y+3);}
    c.fillStyle='#536159';c.fillText('MAD de cobertura normalizada',3,10);c.fillText('Profundidad media cruda por base',pad.left+plotW/2-62,h-3);view.canvas._depthMadPoints=[];
    points.forEach(p=>{const x=px(p.mean_depth),y=py(p.mad);c.fillStyle=p.retained?'#347c52':'#a99074';c.beginPath();c.arc(x,y,2.5,0,Math.PI*2);c.fill();view.canvas._depthMadPoints.push({x,y,p});});
    if(!points.length){c.fillStyle='#68766e';c.fillText('No hay intervalos con métricas para estos filtros.',pad.left+8,pad.top+18);}
    view.canvas.title='Cada punto representa un intervalo; pasa el cursor para ver coordenadas y valores.';
    if(!view.canvas.dataset.depthMadBound){view.canvas.dataset.depthMadBound='true';
      view.canvas.addEventListener('mousemove',event=>{const rect=view.canvas.getBoundingClientRect(),x=(event.clientX-rect.left)*view.canvas.width/rect.width/(window.devicePixelRatio||1),y=(event.clientY-rect.top)*view.canvas.height/rect.height/(window.devicePixelRatio||1);let nearest=null,distance=80;(view.canvas._depthMadPoints||[]).forEach(item=>{const d=Math.hypot(item.x-x,item.y-y);if(d<distance){distance=d;nearest=item;}});view.canvas.title=nearest?`${intervalTooltip(nearest.p)} · profundidad media ${fmt(nearest.p.mean_depth)} · MAD ${fmt(nearest.p.mad)} · ${nearest.p.retained?'retenido':'excluido'}`:'Cada punto representa un intervalo.';});
      view.canvas.addEventListener('click',event=>{const rect=view.canvas.getBoundingClientRect(),x=(event.clientX-rect.left)*view.canvas.width/rect.width/(window.devicePixelRatio||1),y=(event.clientY-rect.top)*view.canvas.height/rect.height/(window.devicePixelRatio||1);let nearest=null,distance=80;(view.canvas._depthMadPoints||[]).forEach(item=>{const d=Math.hypot(item.x-x,item.y-y);if(d<distance){distance=d;nearest=item;}});if(nearest)showInterval(nearest.p.interval_index);});
    }
  }
  function renderSamples(){const threshold=Number($('#sample-z-threshold')?.value||3),batch=$('#sample-batch-filter')?.value||'',rows=data.samples.filter(s=>!batch||s.batch===batch).slice().sort((a,b)=>(b.unexplained_variance_robust_z??-Infinity)-(a.unexplained_variance_robust_z??-Infinity));const varianceValues=rows.map(x=>x.unexplained_variance_exp_log_psi_derived).filter(Number.isFinite),logScale=$('#sample-scale')?.value==='log';drawHist('sample-variance-histogram',hist(varianceValues),'Exp(log ψ), valor derivado');drawBoxPlot('sample-variance-boxplot',varianceValues,logScale);drawViolin('sample-variance-violin',varianceValues,logScale);const v=ctx('sample-variance-chart',Math.min(460,Math.max(220,rows.length*3)));if(v){const vals=rows.map(x=>x.unexplained_variance_robust_z).filter(Number.isFinite),max=Math.max(...vals.map(Math.abs),threshold,1),step=(v.height-28)/Math.max(rows.length,1);v.context.strokeStyle='#72847a';v.context.beginPath();const x0=145+threshold/max*(v.width-160);v.context.moveTo(x0,8);v.context.lineTo(x0,v.height-16);v.context.stroke();rows.forEach((r,i)=>{if(!Number.isFinite(r.unexplained_variance_robust_z))return;const x=145+r.unexplained_variance_robust_z/max*(v.width-160),y=12+i*step;v.context.fillStyle=r.unexplained_variance_robust_z>=threshold?'#b95d49':'#3f8158';v.context.beginPath();v.context.arc(Math.max(145,x),y,2.5,0,Math.PI*2);v.context.fill();if(rows.length<70){v.context.fillStyle='#607068';v.context.font='8px sans-serif';v.context.fillText(r.sample_id.slice(0,20),2,y+3);}});}
    const body=$('#sample-table-body');body.replaceChildren();rows.forEach(s=>{const tr=document.createElement('tr');tr.innerHTML=`<td>${esc(s.sample_id)}${s.unexplained_variance_robust_z>=threshold?' · ⚑':''}</td><td>${esc(s.batch||'—')}</td><td>${esc(s.kit_name||'—')}</td><td>${esc(s.platform||s.protocol||'—')}</td><td>${fmt(s.unexplained_variance_exp_log_psi_derived)}</td><td>${fmt(s.unexplained_variance_robust_z)}</td><td>${fmt(s.global_read_depth)}</td><td>${fmt(s.average_ploidy)}</td><td>${fmt(s.median_ploidy_gq)}</td><td>${fmt(s.zero_count_fraction*100)}%</td><td>${fmt(s.median_posterior_entropy_normalized)}</td><td>${fmt(s.sample_to_cohort_correlation_qc)}</td>`;tr.addEventListener('click',()=>showSample(s.sample_id));body.append(tr);});}
  async function showSample(id){const box=$('#sample-detail');box.textContent='Cargando…';const url=root.dataset.sampleDetailUrl.replace('__SAMPLE__',encodeURIComponent(id));try{const d=await(await fetch(url)).json();box.innerHTML=`<h3>${esc(id)} · detalle técnico</h3><p>Read depth ${fmt(d.sample.global_read_depth)} · ploidía media ${fmt(d.sample.average_ploidy)} · ceros ${d.zero_interval_count}/${data.summary.original_interval_count} · variabilidad no explicada ${fmt(d.sample.unexplained_variance_exp_log_psi_native)} log ψ (nativo)</p><p>Ploidía por contig: ${d.ploidy.map((c,i)=>`${esc(c)} ${d.ploidy_state[i]??'NA'} (GQ ${fmt(d.ploidy_gq[i])})`).join(' · ')}</p><p>Bias factors: ${Object.entries(d.bias_factors).map(([k,v])=>`${esc(k)} ${fmt(v)}`).join(' · ')||'No disponibles'}</p><p>Ceros ${d.zero_interval_count}; threshold bajo=${d.low_count_threshold}; muestra de batch ${esc(d.sample.batch||'—')}</p><canvas class="diagnostic-chart" id="sample-coverage-detail"></canvas><p>Top incertidumbre: ${d.top_entropy_intervals.map(x=>`${esc(x.contig)}:${x.start}-${x.end} (${fmt(x.entropy_normalized)})`).join(' · ')}</p>`;drawHist('sample-coverage-detail',d.coverage_histogram_log2_cpm,'Distribución log₂(CPM + 1)');}catch(e){box.textContent='Detalle no disponible: '+e.message;}}
  function renderPerformance(){drawLine('elbo-warmup-chart',data.performance.warm_up.points);drawLine('elbo-main-chart',data.performance.main_elbo.points);drawHist('entropy-histogram',data.summary.posterior_entropy_histogram,'Entropía normalizada [0,1]');loadEntropy();}
  async function loadEntropy(){const params=new URLSearchParams({contig:$('#entropy-contig')?.value||'',metric:$('#entropy-metric')?.value||'entropy',top_n:$('#entropy-top-n')?.value||50,aggregation:$('#entropy-aggregation')?.value||'median'});[...($('#entropy-samples')?.selectedOptions||[])].forEach(option=>params.append('sample',option.value));try{const res=await fetch(`${root.dataset.heatmapUrl}?${params}`),d=await res.json();drawHeatmap('entropy-heatmap',d.values,d.intervals.map(i=>`${i.contig}:${i.start}`),d.samples,{max:d.metric==='entropy'?1:6,title:d.note,intervals:d.intervals,onRowClick:row=>{const interval=d.intervals[row];activate('intervals');if(interval?.interval_index!==null)showInterval(interval.interval_index);}});$('#entropy-note').textContent=`${d.note} · ${d.aggregation} · ${d.intervals.length} regiones × ${d.samples.length} muestras.`;}catch(e){console.error(e);}}
  document.querySelectorAll('.entropy-interval-row').forEach(row=>{row.tabIndex=0;row.addEventListener('click',()=>{activate('intervals');showInterval(Number(row.dataset.intervalIndex));});});
  $('#entropy-refresh')?.addEventListener('click',loadEntropy);
  ['interval-contig','interval-status','interval-gene','interval-search','interval-gc-min','interval-gc-max','interval-map-min','interval-map-max','interval-coverage-min','interval-coverage-max','interval-variability-min','interval-variability-max'].forEach(id=>$('#'+id)?.addEventListener('change',()=>{intervalPage=1;loadIntervals();}));
  $('#interval-search')?.addEventListener('input',()=>{clearTimeout(intervalTimer);intervalTimer=setTimeout(()=>{intervalPage=1;loadIntervals();},250);});
  $('#interval-prev')?.addEventListener('click',()=>{intervalPage=Math.max(1,intervalPage-1);loadIntervals();});$('#interval-next')?.addEventListener('click',()=>{intervalPage++;loadIntervals();});
  document.querySelectorAll('.diagnostic-table th[data-sort]').forEach(th=>th.addEventListener('click',()=>{const key=th.dataset.sort;intervalDirection=intervalSort===key&&intervalDirection==='desc'?'asc':'desc';intervalSort=key;intervalPage=1;loadIntervals();}));
  function renderPloidyQuality(){const v=ctx('interval-ploidy-quality',300);if(!v)return;const points=(data.interval_ploidy_quality||[]).filter(point=>Number.isFinite(point.value)),{context:c,width:w,height:h}=v,pad={left:42,right:12,top:18,bottom:30},plotW=w-pad.left-pad.right,plotH=h-pad.top-pad.bottom,contigs=[...new Set(points.map(point=>point.contig))],groups=contigs.map(contig=>points.filter(point=>point.contig===contig));c.strokeStyle='#c6cfca';c.beginPath();c.moveTo(pad.left,pad.top);c.lineTo(pad.left,h-pad.bottom);c.lineTo(w-pad.right,h-pad.bottom);c.stroke();c.font='9px sans-serif';c.fillStyle='#68766e';for(let tick=0;tick<=4;tick++){const value=tick/4,y=pad.top+plotH*(1-value);c.fillText(value.toFixed(2),3,y+3);c.strokeStyle='#edf0ed';c.beginPath();c.moveTo(pad.left,y);c.lineTo(w-pad.right,y);c.stroke();}let offset=0;v.canvas._ploidyQualityPoints=[];groups.forEach((group,index)=>{const span=plotW*group.length/Math.max(points.length,1),contigMax=Math.max(...group.map(point=>point.end),1);group.forEach(point=>{const x=pad.left+offset+((point.start+point.end)/2)/contigMax*span,y=pad.top+(1-Math.max(0,Math.min(1,point.value)))*plotH;c.fillStyle=point.retained?'#347c52':'#a99074';c.beginPath();c.arc(x,y,2.2,0,Math.PI*2);c.fill();v.canvas._ploidyQualityPoints.push({x,y,point});});if(group.length){c.fillStyle='#68766e';c.fillText(contigs[index],pad.left+offset,h-17);}offset+=span;});c.fillStyle='#68766e';c.fillText('Posición genómica por contig',pad.left+plotW/2-65,h-3);c.save();c.translate(12,h/2);c.rotate(-Math.PI/2);c.fillText('Probabilidad posterior media del estado MAP',0,0);c.restore();v.canvas.title='Cada punto es un intervalo; verde = retenido, ocre = filtrado.';if(!v.canvas.dataset.qualityTooltipBound){v.canvas.dataset.qualityTooltipBound='true';v.canvas.addEventListener('mousemove',event=>{const rect=v.canvas.getBoundingClientRect(),x=(event.clientX-rect.left)*v.canvas.width/rect.width/(window.devicePixelRatio||1),y=(event.clientY-rect.top)*v.canvas.height/rect.height/(window.devicePixelRatio||1);let nearest=null,distance=80;(v.canvas._ploidyQualityPoints||[]).forEach(item=>{const d=Math.hypot(item.x-x,item.y-y);if(d<distance){distance=d;nearest=item;}});v.canvas.title=nearest?`${nearest.point.contig}:${nearest.point.start}-${nearest.point.end} · media ${nearest.point.value.toFixed(4)} · ${nearest.point.sample_count} muestras · ${nearest.point.retained?'retenido':'filtrado'}`:'Cada punto es un intervalo; verde = retenido, ocre = filtrado.';});}}
  function renderSampleMatrices(){const batch=$('#sample-batch-filter')?.value||'',order=data.cluster_orders?.ploidy_samples||data.ploidy_heatmap.samples,indexes=order.map(id=>{const i=data.ploidy_heatmap.samples.indexOf(id);return{id,i,sample:data.samples.find(row=>row.sample_id===id)};}).filter(x=>x.i>=0&&(!batch||x.sample?.batch===batch)),sampleRows=indexes.map(x=>x.id),openHeatmapSample=row=>showSample(sampleRows[row]);drawHeatmap('ploidy-gq-heatmap',indexes.map(x=>data.ploidy_heatmap.gq[x.i]),sampleRows,data.ploidy_heatmap.contigs,{max:60,title:'PLOIDY_GQ continuo: 0–60 saturado; gris = NA',onRowClick:openHeatmapSample});drawHeatmap('ploidy-state-heatmap',indexes.map(x=>data.ploidy_heatmap.ploidy[x.i]),sampleRows,data.ploidy_heatmap.contigs,{max:5,categoryColors:{0:'#443b63',1:'#31688e',2:'#21918c',3:'#5ec962',4:'#b8de29',5:'#fde725'},otherCategoryColor:'#242424',title:'Ploidía discreta por muestra y contig; gris = N/D; negro = >5',onRowClick:openHeatmapSample});const biasOrder=data.cluster_orders?.bias_samples||order,biasIndexes=biasOrder.map(id=>{const i=data.ploidy_heatmap.samples.indexOf(id);return{id,i,sample:data.samples.find(row=>row.sample_id===id)};}).filter(x=>x.i>=0&&(!batch||x.sample?.batch===batch)),biasRows=biasIndexes.map(x=>x.id),factorOrder=data.cluster_orders?.bias_factors||data.bias_factor_heatmap.factors,factorIndexes=factorOrder.map(name=>data.bias_factor_heatmap.factors.indexOf(name)).filter(i=>i>=0),loadings=biasIndexes.map(x=>factorIndexes.map(i=>data.bias_factor_heatmap.values[x.i]?.[i]??null)),openBiasSample=row=>showSample(biasRows[row]);drawHeatmap('bias-factor-heatmap',loadings,biasRows,factorOrder,{diverging:true,title:'mu_z_su; paleta divergente centrada en cero',onRowClick:openBiasSample});const distance=loadings.map(a=>loadings.map(b=>{const diffs=a.map((value,i)=>Number.isFinite(value)&&Number.isFinite(b[i])?value-b[i]:null).filter(Number.isFinite);return diffs.length?Math.sqrt(diffs.reduce((sum,x)=>sum+x*x,0)):null;}));drawHeatmap('bias-distance-heatmap',distance,biasRows,biasRows,{title:'Distancia euclídea; batch aparece en tooltip',onRowClick:openBiasSample});}
  $('#sample-batch-filter')?.addEventListener('change',()=>{renderSamples();renderSampleMatrices();});$('#sample-z-threshold')?.addEventListener('input',renderSamples);$('#sample-scale')?.addEventListener('change',renderSamples);
  $('#robust-cv-threshold')?.addEventListener('change',loadIntervals);
  $('#reset-diagnostic-filters')?.addEventListener('click',()=>{['interval-contig','interval-status','interval-gene','interval-search','interval-gc-min','interval-gc-max','interval-map-min','interval-map-max','interval-coverage-min','interval-coverage-max','interval-variability-min','interval-variability-max'].forEach(id=>{if($('#'+id))$('#'+id).value='';});intervalPage=1;loadIntervals();persistFilters();});
  $('#interval-export')?.addEventListener('click',event=>{event.preventDefault();const params=intervalParams();params.delete('page');params.delete('page_size');window.location=`${root.dataset.intervalsUrl}.csv?${params}`;});
  const persistFilters=()=>{const saved={};['interval-contig','interval-status','interval-gene','interval-gc-min','interval-gc-max','interval-map-min','interval-map-max','interval-coverage-min','interval-coverage-max','interval-variability-min','interval-variability-max','interval-search','sample-batch-filter','sample-z-threshold','sample-scale','entropy-contig','entropy-metric','entropy-top-n','entropy-aggregation','entropy-samples'].forEach(id=>{const el=document.getElementById(id);if(el)saved[id]=el.multiple?[...el.selectedOptions].map(option=>option.value):el.value;});sessionStorage.setItem(sessionFilterKey,JSON.stringify(saved));};
  root.querySelectorAll('select,input').forEach(el=>el.addEventListener('change',persistFilters));$('#interval-search')?.addEventListener('input',persistFilters);$('#interval-gene')?.addEventListener('input',()=>{clearTimeout(intervalTimer);intervalTimer=setTimeout(()=>{intervalPage=1;loadIntervals();persistFilters();},250);});
  function fmt(x){return Number.isFinite(Number(x))&&x!==null?Number(x).toPrecision(4):'—';}function esc(x){return String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  if($('#sample-table-body')) { renderSamples();renderSampleMatrices();renderPerformance(); }
});

function renderModelQuality(data) {
  const ns = 'http://www.w3.org/2000/svg';
  const svgBase = (target, width, height, label) => {
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', label);
    target.replaceChildren(svg); return svg;
  };
  const label = (svg, x, y, content, anchor = 'start') => {
    const node = document.createElementNS(ns, 'text'); node.setAttribute('x', x); node.setAttribute('y', y);
    node.setAttribute('text-anchor', anchor); node.setAttribute('class', 'quality-chart-label'); node.textContent = content; svg.appendChild(node);
  };
  const heatTarget = document.getElementById('model-ploidy-heatmap');
  if (heatTarget && data.ploidy?.length) {
    const samples = [...new Set(data.ploidy.map((row) => row.sample || 'sample'))].slice(0, 50);
    const contigs = [...new Set(data.ploidy.map((row) => row.contig || '?'))].slice(0, 60);
    const cell = 22, left = 110, top = 35, width = left + contigs.length * cell + 8, height = top + samples.length * cell + 8;
    const svg = svgBase(heatTarget, width, height, 'Heatmap de ploidía por muestra y contig');
    contigs.forEach((contig, index) => label(svg, left + index * cell + 10, 27, contig, 'end'));
    samples.forEach((sample, rowIndex) => {
      label(svg, left - 6, top + rowIndex * cell + 15, sample.slice(0, 16), 'end');
      contigs.forEach((contig, colIndex) => {
        const item = data.ploidy.find((entry) => entry.sample === sample && entry.contig === contig);
        const rect = document.createElementNS(ns, 'rect'); rect.setAttribute('x', left + colIndex * cell);
        rect.setAttribute('y', top + rowIndex * cell); rect.setAttribute('width', cell - 2); rect.setAttribute('height', cell - 2);
        rect.setAttribute('rx', 3); rect.setAttribute('fill', item?.ploidy == null ? '#edf1ef' : (item.ploidy <= 0 ? '#d8dfe0' : item.ploidy === 1 ? '#b9d8c0' : item.ploidy === 2 ? '#6ca47a' : '#32704b'));
        const title = document.createElementNS(ns, 'title'); title.textContent = item ? `${sample} · ${contig}: ploidy ${item.ploidy ?? 'NA'}, PLOIDY_GQ ${item.gq ?? 'NA'}` : `${sample} · ${contig}: sin dato`;
        rect.appendChild(title); svg.appendChild(rect);
      });
    });
  }
  const depthTarget = document.getElementById('model-depth-chart');
  const depths = (data.depth || []).filter((row) => Number.isFinite(row.global_read_depth));
  if (depthTarget && depths.length) {
    const width = 720, height = 210, left = 42, top = 12, plotW = width - left - 12, plotH = height - 35;
    const svg = svgBase(depthTarget, width, height, 'Global read depth por muestra');
    const max = Math.max(...depths.map((row) => row.global_read_depth), 1), barW = plotW / depths.length;
    depths.forEach((row, index) => {
      const h = row.global_read_depth / max * plotH;
      const rect = document.createElementNS(ns, 'rect'); rect.setAttribute('x', left + index * barW + 1); rect.setAttribute('y', top + plotH - h);
      rect.setAttribute('width', Math.max(1, barW - 2)); rect.setAttribute('height', h); rect.setAttribute('fill', '#568ba2');
      const title = document.createElementNS(ns, 'title'); title.textContent = `${row.sample}: ${row.global_read_depth}`; rect.appendChild(title); svg.appendChild(rect);
    });
    label(svg, 4, 12, String(max.toPrecision(3)));
  }
  const convergenceTarget = document.getElementById('model-snr-chart');
  const rows = data.convergence || [];
  const snrKey = rows.flatMap((row) => Object.keys(row)).find((key) => key.includes('snr') && rows.some((row) => Number.isFinite(row[key])));
  if (convergenceTarget && snrKey) {
    const values = rows.map((row, index) => ({ x: Number.isFinite(row.epoch) ? row.epoch : Number.isFinite(row.iteration) ? row.iteration : index + 1, y: row[snrKey] })).filter((point) => Number.isFinite(point.y));
    if (values.length > 1) {
      const width = 720, height = 230, pad = 28, minX = Math.min(...values.map((p) => p.x)), maxX = Math.max(...values.map((p) => p.x));
      const minY = Math.min(...values.map((p) => p.y)), maxY = Math.max(...values.map((p) => p.y));
      const px = (x) => pad + (x - minX) / (maxX - minX || 1) * (width - pad * 2);
      const py = (y) => height - pad - (y - minY) / (maxY - minY || 1) * (height - pad * 2);
      const svg = svgBase(convergenceTarget, width, height, `Training SNR: ${snrKey}`);
      const line = document.createElementNS(ns, 'polyline'); line.setAttribute('points', values.map((point) => `${px(point.x)},${py(point.y)}`).join(' '));
      line.setAttribute('fill', 'none'); line.setAttribute('stroke', '#43865c'); line.setAttribute('stroke-width', '2'); svg.appendChild(line);
      label(svg, pad, height - 4, 'Época / iteración'); label(svg, width - pad, 16, snrKey, 'end');
    }
  }
}

async function transferBamBatch(form) {
  const filesInput = form.querySelector('input[name="alignment_files"]');
  const panel = document.getElementById('bam-transfer-panel');
  const progress = document.getElementById('bam-transfer-progress');
  const status = document.getElementById('bam-transfer-status');
  const count = document.getElementById('bam-transfer-count');
  const log = document.getElementById('bam-transfer-log');
  const submit = form.querySelector('button[type="submit"]');
  const selectedFiles = Array.from(filesInput?.files || []);
  const bams = selectedFiles.filter((file) => file.name.toLowerCase().endsWith('.bam'));
  const bais = selectedFiles.filter((file) => file.name.toLowerCase().endsWith('.bai'));
  if (!bams.length) { window.alert('Selecciona al menos un fichero BAM.'); return; }
  if (bams.length + bais.length !== selectedFiles.length) {
    window.alert('El selector solo admite ficheros .bam y .bai.'); return;
  }

  const sampleFromBai = (name) => {
    const stem = name.toLowerCase().endsWith('.bam.bai') ? name.slice(0, -8) :
      name.toLowerCase().endsWith('.bai') ? name.slice(0, -4) : '';
    return stem.split('.', 1)[0];
  };
  const sampleFromBam = (name) => name.toLowerCase().endsWith('.bam') ? name.slice(0, -4).split('.', 1)[0] : '';
  const indexBySample = new Map();
  for (const bai of bais) {
    const sampleId = sampleFromBai(bai.name);
    if (!sampleId) { window.alert(`Nombre de BAI no válido: ${bai.name}.`); return; }
    if (indexBySample.has(sampleId)) {
      window.alert(`Hay más de un BAI para ${sampleId}. Selecciona un único índice por BAM.`); return;
    }
    indexBySample.set(sampleId, bai);
  }
  const pairs = [];
  const seenSampleIds = new Set();
  const bamIds = new Set(bams.map((bam) => sampleFromBam(bam.name)));
  const pairMismatches = bams
    .filter((bam) => !indexBySample.has(sampleFromBam(bam.name)))
    .map((bam) => `muestra ${sampleFromBam(bam.name) || '(ID no válido)'}: BAM ${bam.name} sin BAI correspondiente`);
  bais.forEach((bai) => {
    const sampleId = sampleFromBai(bai.name);
    if (!bamIds.has(sampleId)) pairMismatches.push(`muestra ${sampleId || '(ID no válido)'}: BAI ${bai.name} sin BAM correspondiente`);
  });
  if (pairMismatches.length) {
    window.alert(`No coinciden los pares BAM/BAI:\n${pairMismatches.join('\n')}`);
    return;
  }
  for (const bam of bams) {
    const sampleId = sampleFromBam(bam.name);
    const bai = indexBySample.get(sampleId);
    if (!sampleId || !bai) {
      window.alert(`La muestra ${sampleId || bam.name} no hace match con su BAI.`);
      return;
    }
    if (seenSampleIds.has(sampleId)) {
      window.alert(`Más de un BAM produce el mismo sample ID (${sampleId}) al tomar el texto anterior al primer punto.`);
      return;
    }
    seenSampleIds.add(sampleId);
    pairs.push({ sampleId, bam, bai, bytes: bam.size + bai.size, row: null, bar: null, label: null });
  }
  if (indexBySample.size !== pairs.length || pairs.length !== bais.length) {
    const unmatched = bais.map((bai) => ({ bai, sampleId: sampleFromBai(bai.name) }))
      .filter(({ sampleId }) => !bamIds.has(sampleId));
    const descriptions = unmatched.map(({ bai, sampleId }) => `muestra ${sampleId || '(ID no válido)'}: BAI ${bai.name} sin BAM correspondiente`);
    window.alert(`Hay índices BAI que no hacen match con ningún BAM${descriptions.length ? `:\n${descriptions.join('\n')}` : '.'}`);
    return;
  }

  panel.hidden = false;
  log.replaceChildren();
  progress.value = 0;
  submit.disabled = true;
  const totalBytes = pairs.reduce((sum, pair) => sum + pair.bytes, 0);
  pairs.forEach((pair) => {
    const row = document.createElement('div'); row.className = 'transfer-item';
    const label = document.createElement('span'); label.textContent = `${pair.bam.name} + ${pair.bai.name} · pendiente`;
    const bar = document.createElement('progress'); bar.max = 100; bar.value = 0;
    row.append(label, bar); log.appendChild(row);
    pair.row = row; pair.bar = bar; pair.label = label;
  });

  let uploadSession = null;
  try {
    status.textContent = 'Preparando la sesión de transferencia…';
    const sessionResponse = await fetch(form.dataset.uploadSessionUrl, { method: 'POST', headers: { Accept: 'application/json' } });
    uploadSession = await sessionResponse.json();
    if (!sessionResponse.ok) throw new Error(uploadSession.error || 'No se pudo iniciar la sesión de carga.');
    let completedBytes = 0;
    for (let index = 0; index < pairs.length; index++) {
      const pair = pairs[index];
      pair.row.classList.add('transferring');
      pair.label.textContent = `${pair.bam.name} + ${pair.bai.name} · transfiriendo`;
      status.textContent = `Transfiriendo ${pair.bam.name}`;
      count.textContent = `${index + 1} de ${pairs.length} BAMs`;
      const payload = new FormData();
      payload.append('bam_file', pair.bam, pair.bam.name);
      payload.append('bai_file', pair.bai, pair.bai.name);
      await uploadPair(uploadSession.upload_url, payload, (loaded, total) => {
        const ratio = total ? Math.min(1, loaded / total) : 0;
        pair.bar.value = ratio * 100;
        progress.value = totalBytes ? ((completedBytes + pair.bytes * ratio) / totalBytes) * 100 : 0;
      });
      completedBytes += pair.bytes;
      pair.bar.value = 100;
      pair.row.classList.remove('transferring'); pair.row.classList.add('transferred');
      pair.label.textContent = `${pair.bam.name} + ${pair.bai.name} · transferido`;
      progress.value = totalBytes ? (completedBytes / totalBytes) * 100 : 100;
    }

    status.textContent = 'Transferencia completa. Validando BAMs y preparando el job QC…';
    count.textContent = `${pairs.length} de ${pairs.length} BAMs transferidos`;
    const finalizeData = new FormData(form);
    finalizeData.delete('alignment_files');
    finalizeData.delete('bam_files'); finalizeData.delete('bai_files');
    finalizeData.append('upload_token', uploadSession.token);
    const response = await fetch(form.action, { method: 'POST', body: finalizeData, redirect: 'follow' });
    if (!response.ok) throw new Error(`No se pudo finalizar el lote (HTTP ${response.status}).`);
    status.textContent = 'Validación finalizada. Abriendo el estado del lote…';
    window.location.assign(response.url || window.location.href);
  } catch (error) {
    if (uploadSession?.token) {
      fetch(form.dataset.uploadSessionUrl.replace(/upload-sessions\/?$/, `upload-sessions/${uploadSession.token}`),
        { method: 'DELETE' }).catch(() => {});
    }
    status.textContent = `La carga se detuvo: ${error.message}`;
    status.classList.add('transfer-error');
    submit.disabled = false;
  }
}

function uploadPair(url, payload, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.upload.addEventListener('progress', (event) => onProgress(event.loaded, event.total));
    xhr.addEventListener('load', () => {
      let result = {};
      try { result = JSON.parse(xhr.responseText || '{}'); } catch (_) { /* non-JSON proxy response */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(result);
      else if (xhr.status === 413) reject(new Error('El servidor o proxy rechazó el tamaño de este BAM/BAI; aumenta su límite de carga.'));
      else reject(new Error(result.error || `Error HTTP ${xhr.status} durante la transferencia.`));
    });
    xhr.addEventListener('error', () => reject(new Error('Error de red durante la transferencia.')));
    xhr.addEventListener('abort', () => reject(new Error('Transferencia cancelada.')));
    xhr.send(payload);
  });
}

function renderQcMetricDistributions(samples) {
  const metrics = [
    { id: 'qc-below-30-chart', label: '% bases diana <30X', value: (s) => s.target_coverage?.bases_below_30x_pct, unit: '%' },
    { id: 'qc-below-100-chart', label: '% bases diana <100X', value: (s) => s.target_coverage?.bases_below_100x_pct, unit: '%' },
    { id: 'qc-mean-coverage-chart', label: 'Cobertura media diana', value: (s) => s.collect_hs_metrics?.mean_target_coverage, unit: 'X' },
    { id: 'qc-fold80-chart', label: 'FOLD_80_BASE_PENALTY', value: (s) => s.collect_hs_metrics?.fold_80_base_penalty, unit: 'factor' },
    { id: 'qc-sample-correlation-chart', label: 'SAMPLE_COVERAGE_CORRELATION', value: (s) => s.normalized?.sample_coverage_correlation, unit: 'Pearson' },
    { id: 'qc-outlier-score-chart', label: 'COHORT_OUTLIER_SCORE', value: (s) => s.normalized?.cohort_outlier_score, unit: 'z robusto' },
  ];
  metrics.forEach((metric) => {
    const target = document.getElementById(metric.id);
    if (!target) return;
    const values = (samples || []).map((sample) => {
      const raw = metric.value(sample);
      return { sample, value: raw === null || raw === undefined || raw === '' ? NaN : Number(raw) };
    }).filter((item) => Number.isFinite(item.value));
    if (!values.length) {
      target.textContent = 'Métrica no disponible para las muestras analizadas.';
      return;
    }
    const width = 420, height = 190, left = 38, right = 12, top = 12, bottom = 30;
    const plotW = width - left - right, plotH = height - top - bottom;
    const min = Math.min(...values.map((item) => item.value));
    const max = Math.max(...values.map((item) => item.value));
    const span = max - min || 1;
    const binCount = min === max ? 1 : Math.min(values.length, 40, Math.max(10, Math.ceil(Math.sqrt(values.length) * 2)));
    const bins = Array.from({ length: binCount }, (_, index) => ({
      start: min === max ? min : min + span * index / binCount,
      end: min === max ? max : min + span * (index + 1) / binCount,
      count: 0,
      samples: [],
    }));
    values.forEach((item) => {
      const index = min === max ? 0 : Math.min(binCount - 1, Math.floor((item.value - min) / span * binCount));
      bins[index].count += 1;
      if (bins[index].samples.length < 5) bins[index].samples.push(item.sample.sample_id);
    });
    const maxCount = Math.max(...bins.map((bin) => bin.count), 1);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', `${metric.label}; distribución de ${values.length} muestras, unidad ${metric.unit}`);
    svg.appendChild(svgLine(left, top + plotH, width - right, top + plotH));
    svg.appendChild(svgLine(left, top, left, top + plotH));
    const barWidth = plotW / binCount;
    bins.forEach((bin, index) => {
      const barHeight = bin.count / maxCount * plotH;
      const rect = document.createElementNS(svg.namespaceURI, 'rect');
      rect.setAttribute('x', String(left + index * barWidth + 1));
      rect.setAttribute('y', String(top + plotH - barHeight));
      rect.setAttribute('width', String(Math.max(1, barWidth - 2)));
      rect.setAttribute('height', String(barHeight));
      rect.setAttribute('fill', '#568ba2');
      const title = document.createElementNS(svg.namespaceURI, 'title');
      title.textContent = `${bin.start.toPrecision(4)}–${bin.end.toPrecision(4)} ${metric.unit}: ${bin.count} muestra(s)${bin.samples.length ? `; p. ej. ${bin.samples.join(', ')}` : ''}`;
      rect.appendChild(title);
      svg.appendChild(rect);
    });
    svg.appendChild(svgText(left, height - 7, `${min.toPrecision(4)} ${metric.unit}`, 'chart-label'));
    const maxLabel = svgText(width - right, height - 7, `${max.toPrecision(4)} ${metric.unit}`, 'chart-label');
    maxLabel.setAttribute('text-anchor', 'end');
    svg.appendChild(maxLabel);
    target.replaceChildren(svg);
    const note = document.createElement('small');
    note.textContent = `${values.length} muestra(s) con dato · ${samples.length - values.length} sin dato`;
    target.appendChild(note);
  });
}

function renderQcCorrelation(data, clusteredOrder) {
  const target = document.getElementById('qc-correlation-chart');
  if (!target) return;
  if (!data?.labels?.length) { target.textContent = 'Selecciona al menos una muestra para mostrar la correlación.'; return; }
  const lookup = new Map(data.labels.map((label, index) => [label, index]));
  const order = clusteredOrder?.length === data.labels.length ? clusteredOrder : data.labels;
  const indices = order.map((label) => lookup.get(label));
  const n = order.length;
  const values = data.matrix.flat().map(Number).filter(Number.isFinite);
  const minValue = Math.min(...values);
  const maxValue = Math.max(...values);
  const cell = Math.max(18, Math.min(34, 440 / n));
  const left = Math.min(125, Math.max(62, n > 8 ? 120 : 90));
  const top = 14;
  const width = left + n * cell + 12;
  const height = top + n * cell + 48;
  const labels = order;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', 'Matriz de correlación entre muestras');
  const defs = document.createElementNS(svg.namespaceURI, 'defs');
  const gradient = document.createElementNS(svg.namespaceURI, 'linearGradient');
  gradient.setAttribute('id', 'qc-viridis-gradient');
  gradient.setAttribute('x1', '0%'); gradient.setAttribute('x2', '100%');
  [[0, '#440154'], [25, '#3b528b'], [50, '#21918c'], [75, '#5ec962'], [100, '#fde725']].forEach(([offset, color]) => {
    const stop = document.createElementNS(svg.namespaceURI, 'stop');
    stop.setAttribute('offset', `${offset}%`); stop.setAttribute('stop-color', color);
    gradient.appendChild(stop);
  });
  defs.appendChild(gradient); svg.appendChild(defs);
  const viridis = ['#440154', '#3b528b', '#21918c', '#5ec962', '#fde725'];
  const colorAt = (value) => {
    const t = maxValue === minValue ? 0.5 : Math.max(0, Math.min(1, (value - minValue) / (maxValue - minValue)));
    const segment = t * (viridis.length - 1), index = Math.min(viridis.length - 2, Math.floor(segment));
    const fraction = segment - index;
    const rgb = (hex) => hex.match(/[0-9a-f]{2}/gi).map((part) => parseInt(part, 16));
    const a = rgb(viridis[index]), b = rgb(viridis[index + 1]);
    return `rgb(${a.map((channel, i) => Math.round(channel + (b[i] - channel) * fraction)).join(',')})`;
  };
  labels.forEach((label, i) => {
    const y = top + i * cell;
    svg.appendChild(svgText(4, y + cell * 0.68, label, 'chart-label'));
    labels.forEach((column, j) => {
      const value = Number(data.matrix?.[indices[i]]?.[indices[j]] ?? 0);
      const rect = document.createElementNS(svg.namespaceURI, 'rect');
      rect.setAttribute('x', String(left + j * cell)); rect.setAttribute('y', String(y));
      rect.setAttribute('width', String(cell - 1)); rect.setAttribute('height', String(cell - 1));
      rect.setAttribute('fill', colorAt(value));
      const title = document.createElementNS(svg.namespaceURI, 'title'); title.textContent = `${labels[i]} × ${column}: ${value.toFixed(4)}`;
      rect.appendChild(title); svg.appendChild(rect);
    });
  });
  const legendY = top + n * cell + 17;
  const legendX = left;
  const legendWidth = Math.min(220, Math.max(100, n * cell));
  const legendBar = document.createElementNS(svg.namespaceURI, 'rect');
  legendBar.setAttribute('x', String(legendX)); legendBar.setAttribute('y', String(legendY));
  legendBar.setAttribute('width', String(legendWidth)); legendBar.setAttribute('height', '10');
  legendBar.setAttribute('fill', 'url(#qc-viridis-gradient)'); svg.appendChild(legendBar);
  svg.appendChild(svgText(legendX, legendY + 25, minValue.toFixed(3), 'chart-label'));
  const maxLabel = svgText(legendX + legendWidth, legendY + 25, maxValue.toFixed(3), 'chart-label');
  maxLabel.setAttribute('text-anchor', 'end'); svg.appendChild(maxLabel);
  const legendTitle = svgText(legendX + legendWidth / 2, legendY - 4, 'Correlación de Pearson', 'chart-label');
  legendTitle.setAttribute('text-anchor', 'middle'); svg.appendChild(legendTitle);
  target.replaceChildren(svg);
}

function renderQcPca(data) {
  const target = document.getElementById('qc-pca-chart');
  if (!target) return;
  const points = data?.points || [];
  if (!points.length) { target.textContent = 'Se necesitan al menos dos muestras para calcular PCA.'; return; }
  const width = 480, height = 300, pad = 42;
  const xs = points.map((item) => Number(item.pc1) || 0);
  const ys = points.map((item) => Number(item.pc2) || 0);
  const xMin = Math.min(...xs), xMax = Math.max(...xs), yMin = Math.min(...ys), yMax = Math.max(...ys);
  const scale = (v, min, max, span) => min === max ? span / 2 : ((v - min) / (max - min)) * span;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.appendChild(svgLine(pad, height - pad, width - 12, height - pad));
  svg.appendChild(svgLine(pad, 12, pad, height - pad));
  points.forEach((point, index) => {
    const cx = pad + scale(Number(point.pc1) || 0, xMin, xMax, width - pad - 28);
    const cy = height - pad - scale(Number(point.pc2) || 0, yMin, yMax, height - pad - 30);
    const dot = document.createElementNS(svg.namespaceURI, 'circle');
    dot.setAttribute('cx', String(cx)); dot.setAttribute('cy', String(cy)); dot.setAttribute('r', '5');
    dot.setAttribute('fill', point.outlier_review ? '#c26058' : '#568ba2');
    const title = document.createElementNS(svg.namespaceURI, 'title');
    title.textContent = `${point.sample_id}${point.cohort_outlier_score == null ? '' : ` · outlier score ${Number(point.cohort_outlier_score).toFixed(2)}`}`;
    dot.appendChild(title); svg.appendChild(dot);
    const label = svgText(cx + 7, cy - 5, point.sample_id, 'chart-label'); svg.appendChild(label);
  });
  const variance = data?.explained_variance || [];
  target.replaceChildren(svg);
  const note = document.createElement('small');
  note.textContent = `PC1 ${variance[0] == null ? '' : (variance[0] * 100).toFixed(1) + '%'} · PC2 ${variance[1] == null ? '' : (variance[1] * 100).toFixed(1) + '%'} · ${data.interval_count || 0} intervalos aptos sin X/Y · Rojo: revisión`;
  target.appendChild(note);
}

function setupQcDownloads(getQc, projectName) {
  const filenameBase = String(projectName).normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
    .replace(/[^A-Za-z0-9._-]+/g, '_').replace(/^_+|_+$/g, '') || 'cohorte';
  document.querySelectorAll('[data-qc-export]').forEach((button) => {
    const [chart, format] = button.dataset.qcExport.split('-');
    button.addEventListener('click', () => {
      const qc = getQc();
      const containerId = chart === 'pca' ? 'qc-pca-chart' : 'qc-correlation-chart';
      const svg = document.querySelector(`#${containerId} svg`);
      if (!svg) return;
      if (format === 'csv') {
        const contents = chart === 'pca' ? qcPcaCsv(qc.pca) : qcCorrelationCsv(qc.correlation, qc.cluster_order);
        downloadQcFile(`${filenameBase}-${chart === 'pca' ? 'pca-points' : 'correlation-heatmap'}.csv`,
          `\uFEFF${contents}`, 'text/csv;charset=utf-8');
        return;
      }
      const svgBlob = qcSvgBlob(svg);
      if (format === 'svg') {
        downloadQcBlob(`${filenameBase}-${chart === 'pca' ? 'pca-without-sex-chromosomes' : 'correlation-heatmap'}.svg`, svgBlob);
      } else {
        downloadQcPng(svgBlob, `${filenameBase}-${chart === 'pca' ? 'pca-without-sex-chromosomes' : 'correlation-heatmap'}.png`);
      }
    });
  });
}

function qcSvgBlob(svg) {
  const clone = svg.cloneNode(true);
  const viewBox = (clone.getAttribute('viewBox') || '0 0 480 300').split(/[ ,]+/).map(Number);
  clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  clone.setAttribute('width', String(viewBox[2]));
  clone.setAttribute('height', String(viewBox[3]));
  const style = document.createElementNS('http://www.w3.org/2000/svg', 'style');
  style.textContent = '.chart-label{font:8px Arial,sans-serif;fill:#68756c}.chart-axis{stroke:#dfe6e0;stroke-width:1}text{font-family:Arial,sans-serif}';
  clone.insertBefore(style, clone.firstChild);
  return new Blob([new XMLSerializer().serializeToString(clone)], { type: 'image/svg+xml;charset=utf-8' });
}

function downloadQcPng(svgBlob, filename) {
  const url = URL.createObjectURL(svgBlob);
  const image = new Image();
  image.onload = () => {
    const scale = 2;
    const canvas = document.createElement('canvas');
    canvas.width = image.naturalWidth * scale;
    canvas.height = image.naturalHeight * scale;
    const context = canvas.getContext('2d');
    context.fillStyle = '#ffffff';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    canvas.toBlob((blob) => {
      if (blob) downloadQcBlob(filename, blob);
      URL.revokeObjectURL(url);
    }, 'image/png');
  };
  image.onerror = () => URL.revokeObjectURL(url);
  image.src = url;
}

function qcCorrelationCsv(data, clusteredOrder) {
  const labels = data?.labels || [];
  const order = clusteredOrder?.length === labels.length ? clusteredOrder : labels;
  const index = new Map(labels.map((label, i) => [label, i]));
  const rows = [['sample_id', ...order]];
  order.forEach((rowLabel) => {
    const rowIndex = index.get(rowLabel);
    rows.push([rowLabel, ...order.map((columnLabel) => data.matrix?.[rowIndex]?.[index.get(columnLabel)] ?? '')]);
  });
  return rows.map((row) => row.map(csvEscape).join(',')).join('\r\n');
}

function qcPcaCsv(data) {
  const variance = data?.explained_variance || [];
  const rows = [[
    'sample_id', 'batch', 'pc1', 'pc2', 'pc3', 'pc1_explained_variance_pct',
    'pc2_explained_variance_pct', 'sample_coverage_correlation', 'cohort_outlier_score',
    'outlier_review', 'interval_count_without_sex_chromosomes', 'excluded_sex_chromosome_interval_count'
  ]];
  (data?.points || []).forEach((point) => rows.push([
    point.sample_id, point.batch, point.pc1, point.pc2 ?? '', point.pc3 ?? '',
    variance[0] == null ? '' : variance[0] * 100,
    variance[1] == null ? '' : variance[1] * 100,
    point.sample_coverage_correlation ?? '', point.cohort_outlier_score ?? '', Boolean(point.outlier_review),
    data.interval_count || 0, data.excluded_sex_chromosome_interval_count || 0
  ]));
  return rows.map((row) => row.map(csvEscape).join(',')).join('\r\n');
}

function csvEscape(value) {
  const text = String(value ?? '');
  return `"${text.replace(/"/g, '""')}"`;
}

function downloadQcFile(filename, contents, type) {
  downloadQcBlob(filename, new Blob([contents], { type }));
}

function downloadQcBlob(filename, blob) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function svgText(x, y, value, className) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  node.setAttribute('x', String(x)); node.setAttribute('y', String(y)); node.setAttribute('class', className);
  node.textContent = value; return node;
}

function svgLine(x1, y1, x2, y2) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  node.setAttribute('x1', String(x1)); node.setAttribute('y1', String(y1));
  node.setAttribute('x2', String(x2)); node.setAttribute('y2', String(y2));
  node.setAttribute('class', 'chart-axis'); return node;
}

function renderIntervalTable(records) {
  const body = document.getElementById('interval-table-body');
  if (!body) return;
  syncIntervalTableHeader(body);
  const pageSize = 100;
  let page = 0;
  const controls = {
    gcMin: document.getElementById('filter-gc-min'),
    gcMax: document.getElementById('filter-gc-max'),
    mapMax: document.getElementById('filter-map-max'),
  };
  const count = document.getElementById('interval-filter-count');
  const pageLabel = document.getElementById('interval-table-page');
  const previous = document.getElementById('interval-page-prev');
  const next = document.getElementById('interval-page-next');
  const numberOrNull = (input) => input && input.value.trim() !== '' && Number.isFinite(Number(input.value)) ? Number(input.value) : null;

  const update = () => {
    const gcMin = numberOrNull(controls.gcMin);
    const gcMax = numberOrNull(controls.gcMax);
    const mapMax = numberOrNull(controls.mapMax);
    const filtered = records.filter((row) =>
      (gcMin === null || row.gc * 100 > gcMin) &&
      (gcMax === null || row.gc * 100 < gcMax) &&
      (mapMax === null || (Number.isFinite(row.mappability) && row.mappability < mapMax))
    );
    const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
    page = Math.min(page, pageCount - 1);
    const visible = filtered.slice(page * pageSize, (page + 1) * pageSize);
    body.replaceChildren();
    if (!visible.length) {
      const row = document.createElement('tr');
      const cell = document.createElement('td');
      cell.colSpan = 4;
      cell.className = 'interval-empty';
      cell.textContent = 'No hay intervalos que coincidan con los filtros.';
      row.appendChild(cell);
      body.appendChild(row);
    } else {
      visible.forEach((record) => {
        const row = document.createElement('tr');
        const values = [
          `${record.chrom}:${Number(record.start).toLocaleString()}-${Number(record.end).toLocaleString()}`,
          record.name || '—',
          Number.isFinite(record.mappability) ? record.mappability.toFixed(4) : '—',
          Number.isFinite(record.gc) ? `${(record.gc * 100).toFixed(2)}%` : '—',
        ];
        values.forEach((value, index) => {
          const cell = document.createElement('td');
          cell.textContent = value;
          if (index === 1) cell.title = value;
          row.appendChild(cell);
        });
        body.appendChild(row);
      });
    }
    count.textContent = `${filtered.length.toLocaleString()} / ${records.length.toLocaleString()} intervalos`;
    pageLabel.textContent = `Página ${page + 1} de ${pageCount}`;
    previous.disabled = page === 0;
    next.disabled = page >= pageCount - 1;
  };

  Object.values(controls).filter(Boolean).forEach((input) => input.addEventListener('input', () => { page = 0; update(); }));
  document.querySelectorAll('[data-clear-filter]').forEach((button) => button.addEventListener('click', () => {
    const input = document.getElementById(button.dataset.clearFilter);
    if (input) { input.value = ''; page = 0; update(); }
  }));
  document.getElementById('clear-interval-filters')?.addEventListener('click', () => {
    Object.values(controls).filter(Boolean).forEach((input) => { input.value = ''; });
    page = 0;
    update();
  });
  previous.addEventListener('click', () => { if (page > 0) { page--; update(); } });
  next.addEventListener('click', () => { page++; update(); });
  update();
}

function syncIntervalTableHeader(body) {
  const table = body.closest('table');
  if (!table) return;
  const headings = ['Intervalo', 'Gen', 'Mappability', 'Contenido GC'];
  const headerRow = table.tHead?.rows[0];
  if (headerRow && [...headerRow.cells].map((cell) => cell.textContent.trim()).join('|') !== headings.join('|')) {
    headerRow.replaceChildren(...headings.map((heading) => {
      const cell = document.createElement('th');
      cell.scope = 'col';
      cell.textContent = heading;
      return cell;
    }));
  }
  let columns = table.querySelector('colgroup');
  if (!columns) {
    columns = document.createElement('colgroup');
    table.insertBefore(columns, table.firstChild);
  }
  const widths = [38, 16, 22, 24];
  columns.replaceChildren(...widths.map((width, index) => {
    const column = document.createElement('col');
    column.className = ['interval-col', 'gene-col', 'mappability-col', 'gc-col'][index];
    column.style.width = `${width}%`;
    return column;
  }));
}

const NS = 'http://www.w3.org/2000/svg';
function svgElement(name, attrs = {}) {
  const node = document.createElementNS(NS, name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
}
function svgLabel(svg, x, y, value, anchor = 'middle') {
  const text = svgElement('text', { x, y, 'text-anchor': anchor, class: 'chart-label' });
  text.textContent = value;
  svg.appendChild(text);
}
function renderHistogram(id, values, requestedBins, unit) {
  const svg = document.getElementById(id);
  if (!svg || !Array.isArray(values) || !values.length) return;
  svg.replaceChildren();
  const width = 520, height = 190, left = 38, right = 12, top = 12, bottom = 34;
  const low = values.reduce((min, value) => Math.min(min, value), Number.POSITIVE_INFINITY);
  const high = values.reduce((max, value) => Math.max(max, value), Number.NEGATIVE_INFINITY);
  const bins = low === high ? 1 : requestedBins;
  const counts = Array(bins).fill(0);
  values.forEach((value) => counts[low === high ? 0 : Math.min(bins - 1, Math.floor(((value - low) / (high - low)) * bins))]++);
  const maxCount = Math.max(...counts, 1), plotW = width - left - right, plotH = height - top - bottom;
  svg.appendChild(svgElement('line', { x1: left, y1: height - bottom, x2: width - right, y2: height - bottom, class: 'chart-axis' }));
  counts.forEach((count, i) => {
    const gap = 3, barW = plotW / bins - gap, barH = (count / maxCount) * plotH;
    const rect = svgElement('rect', { x: left + i * plotW / bins, y: top + plotH - barH, width: Math.max(1, barW), height: barH, rx: 2, class: 'chart-bar' });
    const title = svgElement('title'); title.textContent = `${count} intervalos`; rect.appendChild(title); svg.appendChild(rect);
  });
  svgLabel(svg, left, height - 11, `${low} ${unit}`, 'start');
  svgLabel(svg, width - right, height - 11, `${high} ${unit}`, 'end');
  svgLabel(svg, 17, top + 6, String(maxCount), 'start');
}
function renderChromosomes(id, chromosomeCounts) {
  const svg = document.getElementById(id);
  if (!svg) return;
  svg.replaceChildren();
  const entries = Object.entries(chromosomeCounts || {}).sort((a, b) => chromosomeOrder(a[0]) - chromosomeOrder(b[0]));
  if (!entries.length) return;
  const width = 520, height = 190, left = 25, right = 10, top = 12, bottom = 42;
  const plotW = width - left - right, plotH = height - top - bottom, maxCount = Math.max(...entries.map((x) => x[1]));
  const slot = plotW / entries.length, barW = Math.max(2, slot * .66);
  entries.forEach(([chrom, count], i) => {
    const barH = count / maxCount * plotH, x = left + i * slot + (slot - barW) / 2;
    const rect = svgElement('rect', { x, y: top + plotH - barH, width: barW, height: barH, rx: 2, class: 'chart-bar' });
    const title = svgElement('title'); title.textContent = `${chrom}: ${count} intervalos`; rect.appendChild(title); svg.appendChild(rect);
    const label = svgElement('text', { x: x + barW / 2, y: height - bottom + 7, transform: `rotate(45 ${x + barW / 2} ${height - bottom + 7})`, class: 'chart-label' });
    label.textContent = chrom; svg.appendChild(label);
  });
}
function chromosomeOrder(chrom) {
  const value = chrom.toLowerCase().replace(/^chr/, '');
  if (value === 'x') return 23;
  if (value === 'y') return 24;
  if (value === 'm' || value === 'mt') return 25;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : 100 + value.charCodeAt(0);
}
function renderGenomeTrack(id, records, metric, color) {
  const svg = document.getElementById(id);
  if (!svg) return;
  const select = document.getElementById('gc-chromosome');
  const chroms = [...new Set(records.map((x) => x.chrom))].sort((a, b) => chromosomeOrder(a) - chromosomeOrder(b));
  if (select && !select.dataset.ready) {
    chroms.forEach((chrom) => { const option = document.createElement('option'); option.value = chrom; option.textContent = chrom; select.appendChild(option); });
    select.dataset.ready = '1';
  }
  const draw = () => {
    svg.replaceChildren();
    const selected = select?.value || '';
    const data = records.filter((row) => (!selected || row.chrom === selected) && Number.isFinite(row[metric]));
    if (!data.length) { svgLabel(svg, 500, 110, 'Anotación no disponible'); return; }
    const width = id === 'gc-chart' ? 1000 : 1000, height = id === 'gc-chart' ? 230 : 210;
    const left = 42, right = 12, top = 12, bottom = 28, plotW = width - left - right, plotH = height - top - bottom;
    [0, .25, .5, .75, 1].forEach((tick) => {
      const y = top + (1 - tick) * plotH;
      svg.appendChild(svgElement('line', { x1: left, y1: y, x2: width - right, y2: y, class: 'chart-gridline' }));
      svgLabel(svg, left - 8, y + 3, tick.toFixed(2), 'end');
    });
    const step = Math.max(1, Math.ceil(data.length / 6000));
    data.forEach((row, index) => {
      if (index % step !== 0) return;
      const x = left + (data.length === 1 ? .5 : index / (data.length - 1)) * plotW;
      const value = Math.max(0, Math.min(1, row[metric]));
      const y = top + (1 - value) * plotH;
      const circle = svgElement('circle', { cx: x, cy: y, r: data.length > 1200 ? 1.4 : 2.5, fill: color, opacity: '.76' });
      const title = svgElement('title');
      title.textContent = `${row.chrom}:${row.start}-${row.end} · Gen: ${row.gene || row.name || 'N/D'} · Isoforma: ${row.transcript_id || 'N/D'} · Exón: ${row.exon_number || 'N/D'} · ${metric}: ${(value * 100).toFixed(2)}%`;
      circle.appendChild(title); svg.appendChild(circle);
    });
    svgLabel(svg, left, height - 7, selected || `${data.length.toLocaleString()} intervalos · orden cromosómico`, 'start');
  };
  if (select && !select.dataset.listener) { select.addEventListener('change', () => {
    renderGenomeTrack('gc-chart', records, 'gc', '#43865c');
    renderGenomeTrack('mappability-interval-chart', records, 'mappability', '#568ba2');
    renderGenomeTrack('segmental-duplication-chart', records, 'segmental_duplication', '#b97845');
  }); select.dataset.listener = '1'; }
  draw();
}
function renderScatter(id, records) {
  const svg = document.getElementById(id);
  if (!svg) return;
  svg.replaceChildren();
  const points = records.filter((x) => Number.isFinite(x.gc) && Number.isFinite(x.mappability));
  if (!points.length) return;
  const width = 520, height = 190, left = 38, right = 12, top = 12, bottom = 30, plotW = width - left - right, plotH = height - top - bottom;
  points.forEach((point, i) => {
    if (i % Math.max(1, Math.ceil(points.length / 5000)) !== 0) return;
    const circle = svgElement('circle', { cx: left + point.gc * plotW, cy: top + (1 - point.mappability) * plotH, r: 2, class: 'scatter-point' });
    const title = svgElement('title'); title.textContent = `${point.chrom}:${point.start}-${point.end} · Gen: ${point.gene || point.name || 'N/D'} · Isoforma: ${point.transcript_id || 'N/D'} · Exón: ${point.exon_number || 'N/D'} · GC ${(point.gc * 100).toFixed(1)}% · mappability ${point.mappability.toFixed(3)}`; circle.appendChild(title); svg.appendChild(circle);
  });
  svg.appendChild(svgElement('line', { x1: left, y1: height - bottom, x2: width - right, y2: height - bottom, class: 'chart-axis' }));
  svgLabel(svg, left, height - 7, 'GC 0%', 'start'); svgLabel(svg, width - right, height - 7, 'GC 100%', 'end');
}
