<template>
  <!--
    数据还没到时的占位骨架。

    为什么需要：页面切换是 300ms 淡入，而接口（尤其带 sync 推导的照护/告警）
    要几百毫秒才返回。没有骨架时，第一帧渲染的是"暂无数据"空态，
    数据到了再"啪"地换成列表 —— 观感上就是"先显示没有，然后突然冒出来"。
    骨架保持与内容相近的高度与行距，替换时不会跳版。
  -->
  <div class="sk-list" :class="{ 'sk-list--fill': fill }">
    <div
      v-for="i in rows"
      :key="i"
      class="sk-row"
      :style="fill ? {} : { width: widths[(i - 1) % widths.length], height: `${barH}px` }"
    />
  </div>
</template>

<script setup>
defineProps({
  rows: { type: Number, default: 3 },
  barH: { type: Number, default: 14 },
  /** 撑满父容器（画布这类"高度由布局决定"的区域用，避免骨架定高、内容变高造成跳版） */
  fill: { type: Boolean, default: false },
})

/** 长短交替，模拟真实文本行的参差感；全一样长会像占位符而不是内容 */
const widths = ['94%', '78%', '88%', '62%', '82%']
</script>

<style scoped>
.sk-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 6px 0;
  /* ⚠️ 延迟 350ms 才淡入：数据通常 300~500ms 就回来了，
     立刻显示骨架会"闪一下"，比不显示还难看。慢到超过 350ms 才值得提示。 */
  animation: sk-appear 160ms ease 350ms both;
}

@keyframes sk-appear {
  from {
    opacity: 0;
  }
  to {
    opacity: 1;
  }
}

.sk-row {
  border-radius: 6px;
  /* 用背景位移做微光，比 opacity 闪烁更安静，也不会在深色/浅色下刺眼 */
  background: linear-gradient(
    90deg,
    var(--c-surface-hover) 25%,
    var(--c-line-soft) 37%,
    var(--c-surface-hover) 63%
  );
  background-size: 400% 100%;
  animation: sk-shimmer 1.4s ease-in-out infinite;
}

@keyframes sk-shimmer {
  0% {
    background-position: 100% 0;
  }
  100% {
    background-position: 0 0;
  }
}

/* 撑满模式：一块灰块占满父容器，宽度交给 flex */
.sk-list--fill {
  height: 100%;
}

.sk-list--fill .sk-row {
  flex: 1;
  width: 100%;
}

/* 系统开了"减少动态效果"就不闪，只留灰块 */
@media (prefers-reduced-motion: reduce) {
  .sk-row {
    animation: none;
  }
}
</style>
