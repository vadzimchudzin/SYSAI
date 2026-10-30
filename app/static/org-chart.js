(() => {
  const viewport = document.getElementById('orgViewport');
  const stage = document.getElementById('orgStage');
  if (!viewport || !stage) return;

  const zoomValue = document.getElementById('orgZoomValue');
  const roster = document.getElementById('orgRosterList');
  const dialog = document.getElementById('orgEmployeeDialog');
  const form = document.getElementById('orgEmployeeForm');
  const errorBox = document.getElementById('orgEmployeeError');
  const submitButton = document.getElementById('orgEmployeeSubmit');
  const toast = document.getElementById('orgToast');

  let zoom = 1;
  let panX = 0;
  let panY = 0;
  let interacted = false;
  let toastTimer = null;
  const pointers = new Map();
  let panPointer = null;
  let panStart = null;
  let pinchStart = null;
  let draggedEmployeeId = null;
  let draggedSourceDepartmentId = null;

  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
  const render = () => {
    stage.style.transform = 'translate3d(' + panX + 'px,' + panY + 'px,0) scale(' + zoom + ')';
    if (zoomValue) zoomValue.textContent = Math.round(zoom * 100) + '%';
  };
  const zoomAt = (next, x, y) => {
    const value = clamp(next, 0.15, 3);
    const worldX = (x - panX) / zoom;
    const worldY = (y - panY) / zoom;
    zoom = value;
    panX = x - worldX * zoom;
    panY = y - worldY * zoom;
    render();
  };
  const fit = () => {
    const width = Math.max(stage.offsetWidth, 1);
    const height = Math.max(stage.offsetHeight, 1);
    const availableWidth = Math.max(viewport.clientWidth - 44, 1);
    const availableHeight = Math.max(viewport.clientHeight - 76, 1);
    zoom = clamp(Math.min(availableWidth / width, availableHeight / height, 1), 0.15, 1);
    panX = (viewport.clientWidth - width * zoom) / 2;
    panY = Math.max(22, (viewport.clientHeight - height * zoom) / 2);
    render();
  };
  const showToast = (message, isError) => {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.toggle('is-error', Boolean(isError));
    toast.classList.add('is-visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('is-visible'), 3000);
  };
  const api = async (url, payload) => {
    const response = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(payload || {})
    });
    let result = {};
    try { result = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(result.detail || result.message || 'Не удалось сохранить изменения');
    return result;
  };

  document.querySelectorAll('[data-zoom-in]').forEach(button => button.addEventListener('click', () => {
    interacted = true; zoomAt(zoom * 1.16, viewport.clientWidth / 2, viewport.clientHeight / 2);
  }));
  document.querySelectorAll('[data-zoom-out]').forEach(button => button.addEventListener('click', () => {
    interacted = true; zoomAt(zoom / 1.16, viewport.clientWidth / 2, viewport.clientHeight / 2);
  }));
  document.querySelectorAll('[data-zoom-fit]').forEach(button => button.addEventListener('click', () => {
    interacted = true; fit();
  }));
  document.querySelectorAll('[data-zoom-reset]').forEach(button => button.addEventListener('click', () => {
    interacted = true;
    zoom = 1; panX = (viewport.clientWidth - stage.offsetWidth) / 2; panY = 24; render();
  }));

  viewport.addEventListener('wheel', event => {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    interacted = true;
    const rect = viewport.getBoundingClientRect();
    const multiplier = event.deltaY < 0 ? 1.08 : 1 / 1.08;
    zoomAt(zoom * multiplier, event.clientX - rect.left, event.clientY - rect.top);
  }, { passive: false });

  const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const midpoint = (a, b) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
  viewport.addEventListener('pointerdown', event => {
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size === 2) {
      const points = Array.from(pointers.values());
      const center = midpoint(points[0], points[1]);
      const rect = viewport.getBoundingClientRect();
      const x = center.x - rect.left;
      const y = center.y - rect.top;
      pinchStart = {
        distance: Math.max(distance(points[0], points[1]), 1),
        zoom,
        worldX: (x - panX) / zoom,
        worldY: (y - panY) / zoom
      };
      panPointer = null;
      panStart = null;
      interacted = true;
      event.preventDefault();
      return;
    }
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    if (event.target.closest('button,a,input,textarea,select,[draggable="true"],.org-dept-card,.org-manager-node,.org-director-node')) return;
    panPointer = event.pointerId;
    panStart = { x: event.clientX, y: event.clientY, panX, panY };
    viewport.classList.add('is-panning');
    try { viewport.setPointerCapture(event.pointerId); } catch (_) {}
  });
  viewport.addEventListener('pointermove', event => {
    if (pointers.has(event.pointerId)) pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size >= 2 && pinchStart) {
      const points = Array.from(pointers.values());
      const center = midpoint(points[0], points[1]);
      const rect = viewport.getBoundingClientRect();
      const x = center.x - rect.left;
      const y = center.y - rect.top;
      zoom = clamp(pinchStart.zoom * distance(points[0], points[1]) / pinchStart.distance, 0.15, 3);
      panX = x - pinchStart.worldX * zoom;
      panY = y - pinchStart.worldY * zoom;
      render();
      event.preventDefault();
      return;
    }
    if (panPointer === event.pointerId && panStart) {
      panX = panStart.panX + event.clientX - panStart.x;
      panY = panStart.panY + event.clientY - panStart.y;
      interacted = true;
      render();
    }
  });
  const finishPointer = event => {
    pointers.delete(event.pointerId);
    if (pointers.size < 2) pinchStart = null;
    if (panPointer === event.pointerId) {
      panPointer = null; panStart = null; viewport.classList.remove('is-panning');
    }
  };
  viewport.addEventListener('pointerup', finishPointer);
  viewport.addEventListener('pointercancel', finishPointer);
  viewport.addEventListener('lostpointercapture', finishPointer);

  document.addEventListener('dragstart', event => {
    const card = event.target.closest('[data-org-draggable="true"]');
    if (!card) return;
    draggedEmployeeId = card.dataset.employeeId || '';
    draggedSourceDepartmentId = card.dataset.sourceDepartmentId || '';
    if (!draggedEmployeeId) return;
    card.classList.add('is-dragging');
    event.dataTransfer.effectAllowed = draggedSourceDepartmentId ? 'move' : 'copy';
    event.dataTransfer.setData('text/plain', draggedEmployeeId);
    event.dataTransfer.setData('application/x-sysai-employee', draggedEmployeeId);
    event.dataTransfer.setData('application/x-sysai-source-department', draggedSourceDepartmentId);
  });

  document.addEventListener('dragend', event => {
    const card = event.target.closest('[data-org-draggable="true"]');
    if (card) card.classList.remove('is-dragging');
    draggedEmployeeId = null;
    draggedSourceDepartmentId = null;
    document.querySelectorAll('.is-drop-target').forEach(node => node.classList.remove('is-drop-target'));
  });

  const dropTarget = node => node && node.closest('[data-drop-head], [data-drop-department], [data-drop-unassign]');
  document.addEventListener('dragover', event => {
    const target = dropTarget(event.target);
    if (!target || !draggedEmployeeId) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = draggedSourceDepartmentId ? 'move' : 'copy';
    target.classList.add('is-drop-target');
  });
  document.addEventListener('dragleave', event => {
    const target = dropTarget(event.target);
    if (target && !target.contains(event.relatedTarget)) target.classList.remove('is-drop-target');
  });
  document.addEventListener('drop', async event => {
    const target = dropTarget(event.target);
    if (!target) return;
    event.preventDefault();
    target.classList.remove('is-drop-target');

    const employeeId = draggedEmployeeId ||
      event.dataTransfer.getData('application/x-sysai-employee') ||
      event.dataTransfer.getData('text/plain');
    const sourceDepartmentId = draggedSourceDepartmentId ||
      event.dataTransfer.getData('application/x-sysai-source-department');
    if (!employeeId) return;

    target.classList.add('is-saving');
    try {
      if (target.hasAttribute('data-drop-unassign')) {
        if (!sourceDepartmentId) return;
        const result = await api('/api/org/employees/' + employeeId + '/unassign', {
          department_id: Number(sourceDepartmentId)
        });
        showToast(result.message || 'Сотрудник исключён из отдела');
      } else {
        const departmentId = target.dataset.dropHead || target.dataset.dropDepartment;
        if (!departmentId) return;
        const isHead = target.hasAttribute('data-drop-head');
        if (sourceDepartmentId === String(departmentId) && !isHead) return;
        const payload = { is_head: isHead };
        if (sourceDepartmentId && sourceDepartmentId !== String(departmentId)) {
          payload.move_from_department_id = Number(sourceDepartmentId);
        }
        const result = await api('/api/org/departments/' + departmentId + '/members/' + employeeId, payload);
        showToast(result.message || 'Назначение сохранено');
      }
      setTimeout(() => window.location.reload(), 450);
    } catch (error) {
      showToast(error.message, true);
    } finally {
      target.classList.remove('is-saving');
    }
  });

  const search = document.getElementById('orgEmployeeSearch');
  if (search && roster) {
    search.addEventListener('input', () => {
      const value = search.value.trim().toLocaleLowerCase('ru');
      roster.querySelectorAll('.org-roster-item').forEach(card => {
        const haystack = (card.dataset.employeeName || '') + ' ' + (card.dataset.employeePosition || '');
        card.hidden = value && !haystack.includes(value);
      });
    });
  }

  const openDialog = departmentId => {
    if (!dialog || !form) return;
    form.reset();
    form.elements.department_id.value = departmentId || '';
    errorBox.textContent = '';
    const departmentField = document.querySelector('[data-add-department="' + departmentId + '"]');
    const heading = dialog.querySelector('.org-dialog-header h2');
    const intro = dialog.querySelector('.org-dialog-header p');
    if (departmentId) {
      const department = departmentField ? departmentField.closest('.org-dept-card') : null;
      const departmentName = department ? department.querySelector('h3').textContent : 'выбранное подразделение';
      heading.textContent = 'Новый сотрудник';
      intro.textContent = 'Новая карточка будет сразу добавлена в «' + departmentName + '».';
      form.querySelector('.org-form-divider span').textContent = 'Назначение в «' + departmentName + '»';
    } else {
      heading.textContent = 'Новый сотрудник';
      intro.textContent = 'Создаётся карточка в общем справочнике. В отделы её можно назначить перетаскиванием.';
      form.querySelector('.org-form-divider span').textContent = 'Назначение в подразделение (необязательно)';
    }
    dialog.showModal();
    setTimeout(() => form.elements.name.focus(), 40);
  };
  document.querySelectorAll('[data-new-employee]').forEach(button => button.addEventListener('click', () => openDialog('')));
  document.querySelectorAll('[data-add-department]').forEach(button => button.addEventListener('click', () => openDialog(button.dataset.addDepartment)));
  document.querySelectorAll('[data-close-employee]').forEach(button => button.addEventListener('click', () => dialog.close()));
  if (dialog) dialog.addEventListener('click', event => {
    if (event.target === dialog) dialog.close();
  });

  if (form) {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      errorBox.textContent = '';
      submitButton.disabled = true;
      submitButton.textContent = 'Сохраняем…';
      const data = new FormData(form);
      const payload = {
        name: data.get('name'),
        position: data.get('position'),
        aliases: data.get('aliases'),
        telegram_contact: data.get('telegram_contact'),
        competencies: data.get('competencies'),
        responsibilities: data.get('responsibilities'),
        context: data.get('context'),
        department_id: data.get('department_id') || null,
        department_role: data.get('department_role'),
        department_responsibilities: data.get('department_responsibilities'),
        bitrix_user_id: data.get('bitrix_user_id'),
        is_head: form.elements.is_head.checked
      };
      try {
        const result = await api('/api/org/employees', payload);
        dialog.close();
        showToast(result.message || 'Карточка сотрудника создана');
        setTimeout(() => window.location.reload(), 450);
      } catch (error) {
        errorBox.textContent = error.message;
        submitButton.disabled = false;
        submitButton.textContent = 'Создать сотрудника';
      }
    });
  }

  window.addEventListener('resize', () => {
    if (!interacted) fit();
  });
  requestAnimationFrame(() => requestAnimationFrame(fit));
})();
