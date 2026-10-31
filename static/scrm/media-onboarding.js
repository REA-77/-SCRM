(() => {
  if (document.body.dataset.mediaAccount !== '1') return;

  const page = document.body.dataset.active;
  const guides = {
    brands: [
      { target: '.brand-hero', title: '合作总览', text: '这里汇总当前媒介的合作状态、关联商单、展示视频与评论反馈。' },
      { target: '[data-guide="related-deals"]', title: '关联商单', text: '这里列出与当前媒介有关的全部商单，可先查看名称、金额和合作状态。' },
      { target: '[data-guide="deal-open"]', title: '进入商单详情', text: '即将自动打开这条商单，下一页会继续介绍进度、KPI、视频和评论模块。', navigate: true },
      { target: '[data-guide="brand-display-videos"]', title: '展示视频', text: '查看商单发布链接与内容库的匹配状态；无视频列表权限时不会提供全局视频入口。' },
      { target: '[data-guide="brand-comments"]', title: '评论反馈', text: '按购买意向、负面和其他信号查看关联视频的评论反馈。' },
      { target: '[data-guide="brand-original-videos"]', title: '博主原创', text: '请关注更多博主,期待与您的再次合作。' }
    ],
    deals: [
      { target: '[data-guide="deal-status"]', title: '商单状态', text: '顶部展示品牌、媒介和当前执行阶段，用于快速判断商单是否在排期、发布或已完成。' },
      { target: '[data-guide="publish-schedule"]', title: '预计发布', text: '优先显示实际发布时间；未发布时显示计划排期或待排期状态。' },
      { target: '[data-guide="original-video-showcase"]', title: '博主原创', text: '请关注更多博主,期待与您的再次合作。' },
      { target: '[data-guide="deal-information"]', title: '商单资料', text: '集中查看商单名称、品牌产品、负责人、平台账号和当前合作状态。' },
      { target: '[data-guide="display-video-status"]', title: '视频状态', text: '每条展示视频展示平台、账户、金额、KPI 播放/互动、发布时间和是否达标。' },
      { target: '[data-guide="video-kpi-views"]', title: 'KPI 播放', text: '上方是目标播放量，下方“实际”展示该视频当前实际播放数据。' },
      { target: '[data-guide="video-kpi-interactions"]', title: 'KPI 互动', text: '上方是目标互动量，下方“实际”展示点赞、评论等互动的当前完成值。' },
      { target: '[data-guide="video-status"]', title: '视频状态', text: '根据视频的 KPI 播放和互动完成情况，显示“已达标”或“未达标”。' },
      { target: '[data-guide="comment-overview"]', title: '评论状态', text: '这里汇总商单视频评论，并标识每条评论的处理状态。' },
      { target: '[data-guide="comment-intent"]', title: '购买意向', text: '切换到这里查看表达购买兴趣的评论数量和具体内容。' },
      { target: '[data-guide="comment-negative"]', title: '负面反馈', text: '切换到这里查看负面评论及其分类，便于及时关注风险。' },
      { target: '[data-guide="comment-status"]', title: '评论处理状态', text: '通过待处理、已审核和已处理状态，了解每条评论当前的跟进进度。' }
    ]
  };
  const steps = guides[page];
  if (!steps) return;
  const storageKey = `scrm.media-guide.v4.${page}`;

  const style = document.createElement('style');
  style.textContent = `
    .media-guide-mask{position:fixed;inset:0;background:rgba(15,23,18,.52);z-index:1100;opacity:0;transition:opacity .26s ease}.media-guide-mask.show{opacity:1}
    .media-guide-focus{position:relative!important;z-index:1101!important;box-shadow:0 0 0 5px rgba(126,218,148,.92),0 18px 45px rgba(0,0,0,.28)!important;border-radius:14px;animation:mediaGuidePulse 1.65s ease-in-out infinite}
    .media-guide-card{position:fixed;top:50%;left:50%;z-index:1102;width:min(320px,calc(100vw - 32px));padding:18px;border-radius:15px;background:#fff;color:#18221c;box-shadow:0 20px 55px rgba(0,0,0,.3);transform:translate(-50%,-45%);opacity:0;transition:transform .28s ease,opacity .28s ease}.theme-night .media-guide-card{background:#1c261f;color:#ecf7ee}
    .media-guide-card.show{transform:translate(-50%,-50%);opacity:1}.media-guide-kicker{color:#258149;font-size:12px;font-weight:750;letter-spacing:.08em}.media-guide-card h3{margin:5px 0 7px;font-size:18px}.media-guide-card p{margin:0;color:#68756c;font-size:13px;line-height:1.65}.theme-night .media-guide-card p{color:#b5c6b9}
    .media-guide-actions{display:flex;align-items:center;gap:8px;margin-top:15px}.media-guide-actions .skip{margin-right:auto;border:0;background:transparent;color:#849188;cursor:pointer;font:inherit;font-size:12px}.media-guide-actions button:not(.skip){border:0;border-radius:8px;background:#27864b;color:#fff;padding:7px 12px;cursor:pointer;font-weight:650}
    .media-guide-replay{position:fixed;right:22px;bottom:22px;z-index:999;border:0;width:36px;height:36px;border-radius:50%;background:#27864b;color:#fff;box-shadow:0 5px 16px rgba(23,112,57,.35);cursor:pointer;font-weight:800;transition:transform .18s}.media-guide-replay:hover{transform:scale(1.08)}
    @keyframes mediaGuidePulse{50%{box-shadow:0 0 0 8px rgba(126,218,148,.56),0 18px 45px rgba(0,0,0,.28)!important}}
    @media(max-width:700px){.media-guide-replay{right:14px;bottom:14px}}
  `;
  document.head.appendChild(style);

  let activeTarget = null;
  const clearFocus = () => { if (activeTarget) activeTarget.classList.remove('media-guide-focus'); activeTarget = null; };
  const start = () => {
    let index = 0;
    const mask = document.createElement('div'); mask.className = 'media-guide-mask';
    const card = document.createElement('section'); card.className = 'media-guide-card';
    document.body.append(mask, card);
    requestAnimationFrame(() => { mask.classList.add('show'); card.classList.add('show'); });
    let onEscape, navigateTimer;
    const finish = () => {
      clearFocus(); mask.classList.remove('show'); card.classList.remove('show');
      clearTimeout(navigateTimer);
      document.removeEventListener('keydown', onEscape);
      setTimeout(() => { mask.remove(); card.remove(); }, 280);
      try { localStorage.setItem(storageKey, 'done'); } catch (_) {}
    };
    mask.onclick = finish;
    onEscape = event => { if (event.key === 'Escape') finish(); };
    document.addEventListener('keydown', onEscape);
    const render = () => {
      clearFocus();
      const step = steps[index], target = document.querySelector(step.target);
      if (!target) { if (index < steps.length - 1) { index += 1; render(); } else finish(); return; }
      activeTarget = target; target.classList.add('media-guide-focus');
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      if (step.navigate) {
        card.innerHTML = `<div class="media-guide-kicker">新手指引 ${index + 1} / ${steps.length}</div><h3>${step.title}</h3><p>${step.text}</p><div class="media-guide-actions"><button class="skip" type="button">取消自动跳转</button><button type="button" disabled>正在打开…</button></div>`;
        card.querySelector('.skip').onclick = finish;
        const href = target.getAttribute('href');
        navigateTimer = setTimeout(() => {
          if (!href) { finish(); return; }
          try { localStorage.setItem(storageKey, 'done'); } catch (_) {}
          location.assign(href);
        }, 1100);
        return;
      }
      card.innerHTML = `<div class="media-guide-kicker">新手指引 ${index + 1} / ${steps.length}</div><h3>${step.title}</h3><p>${step.text}</p><div class="media-guide-actions"><button class="skip" type="button">跳过</button><button type="button">${index === steps.length - 1 ? '完成' : '下一步'}</button></div>`;
      card.querySelector('.skip').onclick = finish;
      card.querySelector('.media-guide-actions button:last-child').onclick = () => { if (index === steps.length - 1) finish(); else { index += 1; render(); } };
    };
    render();
  };
  const replay = document.createElement('button');
  replay.className = 'media-guide-replay'; replay.type = 'button'; replay.title = '查看功能指引'; replay.setAttribute('aria-label', '查看功能指引'); replay.textContent = '?';
  replay.onclick = start; document.body.appendChild(replay);
  try { if (!localStorage.getItem(storageKey)) setTimeout(start, 650); } catch (_) { setTimeout(start, 650); }
})();
