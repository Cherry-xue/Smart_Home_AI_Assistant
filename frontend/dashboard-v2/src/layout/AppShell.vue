<template>
  <div class="app">
    <!-- ==================== 顶边栏 ==================== -->
    <header class="topbar">
      <div class="topbar-left">
        <!-- 多套房切换。只有一套时不显示下拉，避免"点了没反应"的困惑。 -->
        <el-select
          v-if="home.multiple"
          :model-value="home.currentId"
          size="small"
          class="home-select"
          @update:model-value="onSwitchHome"
        >
          <el-option
            v-for="h in home.homes"
            :key="h.id"
            :label="h.name"
            :value="h.id"
          >
            <span class="home-opt">
              <span>{{ h.name }}</span>
              <span class="home-opt-meta tnum">{{ h.devices }} 台 · {{ h.online }} 在线</span>
            </span>
          </el-option>
        </el-select>
        <span v-else class="home-label">{{ home.currentName }}</span>

        <!-- 套房的增 / 编辑 / 删。放在切换器右边，是因为"管理哪套房"和"选择哪套房"是同一件事 -->
        <button class="home-act" title="新增套房" @click="openCreate">新增</button>
        <button
          class="home-act"
          title="编辑户型图与房间名"
          :disabled="editorLoading"
          @click="openEditor"
        >
          编辑
        </button>
        <el-tooltip
          :content="home.homes.length <= 1 ? '至少要保留一套房' : '删除当前套房（含设备与历史数据）'"
          placement="bottom"
        >
          <span>
            <button
              class="home-act home-act--danger"
              :disabled="home.homes.length <= 1 || deleting"
              @click="onDeleteHome"
            >
              {{ deleting ? '删除中…' : '删除' }}
            </button>
          </span>
        </el-tooltip>
      </div>
      <h1 class="topbar-title">智能家居 AI 助手</h1>
      <div class="topbar-right">
        <button class="round-btn" :title="msgTitle" @click="drawer = true">
          <span>消息</span>
          <i v-if="system.pendingCount > 0" class="dot">{{ system.pendingCount }}</i>
        </button>
      </div>
    </header>

    <!-- ==================== 主体 ==================== -->
    <div class="body">
      <!-- ---------------- 主菜单 ---------------- -->
      <aside class="sidebar">
        <nav class="nav">
          <RouterLink
            v-for="item in navItems"
            :key="item.path"
            :to="item.path"
            class="nav-item"
            :class="{ 'nav-item--active': route.path === item.path }"
          >
            {{ item.title }}
          </RouterLink>
        </nav>

        <div class="sb-foot">
          <div class="sb-foot-row">
            <span class="sb-foot-label">数据时间</span>
            <button class="round-btn round-btn--sm" :class="{ spinning: system.loading }" title="刷新" @click="refresh">
              <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                <path d="M21 12a9 9 0 1 1-2.64-6.36" />
                <path d="M21 3v6h-6" />
              </svg>
            </button>
          </div>
          <div class="sb-foot-value tnum">{{ anchorText }}</div>
          <div class="sb-foot-note">
            <span v-if="system.dataMode" class="tag">{{ modeLabel }}</span>
            <span class="sb-foot-mode">{{ system.mqttConnected ? '链路正常' : '链路断开' }}</span>
          </div>
          <div v-if="system.highRiskAutoExec" class="sb-foot-alert">
            ⚠️ 高风险自动执行已开启（违反红线）
          </div>
        </div>
      </aside>

      <!-- ---------------- 主显示区 ---------------- -->
      <main class="main">
        <!-- ⚠️ mode="out-in"：旧页面先完全离场，新页面才进场 ——
             所以看到的是"下方菜单下沉 → 新下方菜单升起"两段，不会两页抢位置。
             动效的具体规则在 `styles/transitions.css`，页面里只要标注
             `.page-fade`（淡入淡出）和 `.page-slide`（升降）两个钩子类即可。

             ⚠️⚠️ `:duration` **必须显式给**，不能省。
             Vue 判断"离场结束了吗"的办法是**读根元素的 transition 时长**，
             而我们的过渡都挂在**子元素**上（.page-fade / .page-slide），
             根元素自己没有过渡 → Vue 以为瞬间就结束 → 立刻卸载旧页面，
             把子元素的动画一起带走（实测现象：点完导航，旧页面已经没了、动画没播）。
             这里把真实时长报给 Vue，它就会老老实实等。
             改 transitions.css 里的时长时，**这两个数要跟着改**。 -->
        <RouterView v-slot="{ Component, route }">
          <Transition name="page" mode="out-in" :duration="{ enter: 400, leave: 280 }">
            <component :is="Component" :key="route.path" />
          </Transition>
        </RouterView>
      </main>
    </div>

    <!-- ==================== 消息 ==================== -->
    <el-drawer v-model="drawer" title="消息" size="380px" direction="rtl">
      <div v-if="system.pendingCount === 0" class="msg-empty">
        <p>暂无待处理消息。</p>
        <p class="muted">
          需要人工确认的高风险动作（门锁 / 燃气阀 / 电气总闸）会出现在这里。
        </p>
      </div>

      <div v-else class="msg-list">
        <div v-for="(item, i) in system.pending" :key="`${item.thread_id}-${i}`" class="msg-item">
          <div class="msg-item-head">
            <strong>{{ item.device_name || item.device_code }}</strong>
            <span class="tag tag--warn">待确认</span>
          </div>
          <p class="msg-item-body">{{ item.prompt || item.detail || '高风险操作需要人工确认' }}</p>
          <div class="msg-item-meta muted">
            能力 {{ item.capability }} · 目标值 {{ item.value }}
            <template v-if="item.question"> · 来自「{{ item.question }}」</template>
          </div>
          <div class="msg-item-actions">
            <button class="link-btn" :disabled="busy" @click="decide(item, 'approve')">批准（留痕）</button>
            <button class="link-btn link-btn--muted" :disabled="busy" @click="decide(item, 'reject')">驳回</button>
          </div>
        </div>
        <p class="muted msg-foot">
          ⚠️ 批准的含义是「人工已确认并留痕」，**不会**让系统去执行门锁 / 燃气阀 / 总闸的动作。
        </p>
      </div>
    </el-drawer>
    <!-- ==================== 新建套房 ==================== -->
    <el-dialog
      v-model="createOpen"
      title="新建套房"
      width="460px"
      append-to-body
      :close-on-click-modal="false"
    >
      <el-form label-width="76px" @submit.prevent>
        <el-form-item label="套房名称">
          <el-input
            v-model="createForm.name"
            maxlength="64"
            show-word-limit
            placeholder="例如：1 号楼 1201"
            @keyup.enter="submitCreate"
          />
        </el-form-item>
      </el-form>

      <p class="create-hint">
        新套房是空的：<b>没有房间、没有设备、也没有户型图</b>。
        <br />
        建好后点顶栏 <b>「编辑」</b>，在画布上拖一个框、写上名字 ——
        房间就按你写的名字创建，不需要先在这里起名。
      </p>

      <template #footer>
        <el-button @click="createOpen = false">取消</el-button>
        <el-button type="primary" :loading="creating" :disabled="!canCreate" @click="submitCreate">
          创建
        </el-button>
      </template>
    </el-dialog>

    <!-- ==================== 编辑房子（户型图 + 房间名） ====================
         放在顶栏而不是总览页：从任何一页都能直接改户型，不用先切回总览。
         几何与房间在打开时现取，编辑完用 store 里的版本号通知总览页重画。 -->
    <FloorplanEditor
      v-model="editorOpen"
      :home-id="home.currentId"
      :home-name="home.currentName"
      :geometry="editorGeometry"
      :rooms="editorRooms"
      @saved="onFloorplanSaved"
      @rooms-changed="loadEditorRooms"
      @renamed="onHomeRenamed"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { decidePending, getFloorplan, getRooms } from '@/api'
import FloorplanEditor from '@/pages/overview/FloorplanEditor.vue'
import { useHomeStore } from '@/stores/home'
import { useSystemStore } from '@/stores/system'
import { dataModeLabel, fmtDateTime } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const system = useSystemStore()
const home = useHomeStore()

const drawer = ref(false)
const busy = ref(false)

// ---------------------------------------------------------------- 编辑房子
const editorOpen = ref(false)
const editorGeometry = ref(null)
const editorRooms = ref([])
const editorLoading = ref(false)

/**
 * 打开编辑器前**现取一次**几何与房间。
 *
 * 不能直接用总览页那份缓存：别的页面（设备 / 能耗）没有它，
 * 而从顶栏点「编辑」应当在哪一页都能用。
 */
async function openEditor() {
  if (editorLoading.value) return
  editorLoading.value = true
  try {
    const hid = home.currentId
    const [fp, rm] = await Promise.all([
      getFloorplan(hid).catch(() => null),
      getRooms(hid).catch(() => null),
    ])
    editorGeometry.value = fp
    editorRooms.value = rm?.items ?? []
    editorOpen.value = true
  } finally {
    editorLoading.value = false
  }
}

async function loadEditorRooms() {
  try {
    const rm = await getRooms(home.currentId)
    editorRooms.value = rm?.items ?? []
  } catch {
    /* 拉不到就保持旧列表 */
  }
}

/** 编辑器里改了套房名 → 刷新顶栏下拉，名字立刻变 */
async function onHomeRenamed() {
  await home.refresh()
}

function onFloorplanSaved(g) {
  if (g) editorGeometry.value = g
  // 通知总览页重画（顶栏与总览页不是父子，用 store 里的版本号）
  home.bumpFloorplan()
  // 保存时可能新建了房间，下拉选项要跟着变
  loadEditorRooms()
}

// ---------------------------------------------------------------- 套房的增删
const createOpen = ref(false)
const creating = ref(false)
const deleting = ref(false)
const createForm = reactive({ name: '' })

const canCreate = computed(() => !creating.value && !!createForm.name.trim())

/** 每次打开都重置：上次填了一半取消掉的脏数据不能带进来 */
function openCreate() {
  createForm.name = ''
  createOpen.value = true
}

async function submitCreate() {
  if (!canCreate.value) return
  creating.value = true
  try {
    const res = await home.add(createForm.name.trim())
    createOpen.value = false
    ElMessage.success(res.message || `已创建「${res.name}」`)
    // 新套房是空的，落到总览页正好能看到"去编辑户型 / 去添加设备"的提示
    if (route.path !== '/overview') router.push('/overview')
  } catch (e) {
    ElMessage.error(e.message || '创建失败')
  } finally {
    creating.value = false
  }
}

async function onDeleteHome() {
  if (home.homes.length <= 1 || deleting.value) return
  const cur = home.current
  if (!cur) return

  try {
    await ElMessageBox.confirm(
      `「${cur.name}」下的 ${cur.devices} 台设备、${cur.rooms} 个房间，` +
        '以及它们的全部历史数据（读数、事件、告警、能耗、照护记录）都会被永久删除，无法恢复。',
      '删除套房',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return // 用户点了取消
  }

  deleting.value = true
  // 删 1 号房那种带几百万行历史数据的套房，服务端要跑几秒到几十秒。
  // 期间如果只有一个灰掉的按钮，用户会以为卡死或没点上，于是疯狂重复点。
  const tip = ElMessage({
    message: `正在删除「${cur.name}」及其历史数据…数据多时需要几十秒，请不要重复点击`,
    type: 'info',
    duration: 0,
    showClose: false,
  })
  try {
    const res = await home.remove(cur.id)
    ElMessage.success(res.message || `已删除「${res.name}」`)
    // 删的可能就是当前这套：store 会把 currentId 回落到第一套，
    // 但路由还停在被删套房的页面上没有任何意义，统一回总览
    if (route.path !== '/overview') router.push('/overview')
  } catch (e) {
    ElMessage.error(e.message || '删除失败')
  } finally {
    tip.close()
    deleting.value = false
  }
}

/**
 * 切换套房。
 *
 * 只改 store 里的 id，各页面靠 `watch(home.currentId)` 自己重新取数 ——
 * 不在这里手动调每个页面的 load()，否则新增页面时一定会漏。
 * 切完顺手回总览：停在"设备详情"上换了套房，看到的是另一套房的设备，很容易懵。
 */
function onSwitchHome(id) {
  if (id === home.currentId) return
  home.setCurrent(id)
  if (route.path !== '/overview') router.push('/overview')
}

const navItems = [
  { path: '/overview', title: '房屋总览' },
  { path: '/devices', title: '设备' },
  // 照护与安防放一起：告警、活动节律、主动建议本来就是同一条"关心家里的人"的链路
  { path: '/care', title: '照护与安防' },
  { path: '/energy', title: '能耗' },
  { path: '/chat', title: 'AI对话' },
  { path: '/scenes', title: '场景编排' },
  { path: '/knowledge', title: '知识库' },
  { path: '/system', title: '系统状态' },
]

const anchorText = computed(() => fmtDateTime(system.dataAnchor))
const modeLabel = computed(() => dataModeLabel(system.dataMode))
const msgTitle = computed(() =>
  system.pendingCount > 0 ? `${system.pendingCount} 条待确认` : '暂无待确认消息',
)

async function refresh() {
  await system.refreshAll()
  // ⚠️ 这里用绝对时间而不是 fromNow：数据锚点本身可能领先真实时钟，
  //    拿它跟它自己比永远是"0 秒前"，没意义。
  ElMessage({
    message: `已刷新 · 数据时间 ${fmtDateTime(system.dataAnchor)}`,
    type: 'success',
    duration: 1600,
  })
}

async function decide(item, choice) {
  busy.value = true
  try {
    const out = await decidePending(item.thread_id, { choice, interventionIndex: 0 })
    ElMessage({
      message: out?.message || (out?.accepted ? '已记录' : '该确认未被采纳'),
      type: out?.accepted ? 'success' : 'warning',
      duration: 3200,
    })
    await system.refreshPending()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  await Promise.all([system.refreshAll(), home.refresh()])
  system.startPolling()
})
onUnmounted(() => system.stopPolling())
</script>

<style scoped>
.app {
  height: 100%;
  display: flex;
  flex-direction: column;
}

/* ---------------- 顶边栏 ---------------- */
.topbar {
  flex: 0 0 var(--h-topbar);
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  padding: 0 var(--pad);
  border-bottom: 1px solid var(--c-line);
}
.home-label {
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
  letter-spacing: 0.3px;
}

/* 顶栏左区：切换器 + 两个小按钮，横排不换行 */
.topbar-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

/* 套房的增删按钮：刻意做得比切换器"轻"，不抢主控件的注意力 */
.home-act {
  flex: none;
  height: 24px;
  padding: 0 8px;
  border: 1px solid var(--c-line);
  border-radius: 6px;
  background: transparent;
  color: var(--c-ink-soft);
  font-size: var(--fs-tiny);
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
}
.home-act:hover:not(:disabled) {
  color: var(--c-ink);
  border-color: var(--c-ink-muted);
  background: var(--c-surface-hover);
}
.home-act:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.home-act--danger:hover:not(:disabled) {
  color: var(--c-danger, #d9534f);
  border-color: var(--c-danger, #d9534f);
}

.create-hint {
  margin: 0;
  font-size: var(--fs-tiny);
  line-height: 1.7;
  color: var(--c-ink-muted);
}
.create-hint code {
  padding: 0 3px;
  border-radius: 3px;
  background: var(--c-surface-hover);
}
/* 套房切换器：顶栏很窄，控件要收着点 */
.home-select {
  width: 230px;
}
.home-select :deep(.el-select__wrapper) {
  box-shadow: none;
  background: transparent;
  padding-left: 0;
}
.home-select :deep(.el-select__wrapper):hover {
  background: var(--c-surface-hover);
}
.home-opt {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.home-opt-meta {
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
}
.topbar-title {
  margin: 0;
  font-size: var(--fs-title);
  font-weight: 500;
  letter-spacing: 1px;
}
.topbar-right {
  display: flex;
  justify-content: flex-end;
}

/* 圆形按钮（消息 / 刷新） */
.round-btn {
  position: relative;
  width: 40px;
  height: 40px;
  border: 1px solid var(--c-line);
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: var(--fs-tiny);
  color: var(--c-ink-soft);
  transition: border-color var(--dur) var(--ease), color var(--dur) var(--ease);
}
.round-btn:hover {
  border-color: var(--c-accent);
  color: var(--c-accent);
}
.round-btn--sm {
  width: 24px;
  height: 24px;
}
.round-btn.spinning svg {
  animation: spin 900ms linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
.dot {
  position: absolute;
  top: -2px;
  right: -2px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 8px;
  background: var(--c-danger);
  color: #fff;
  font-size: 10px;
  font-style: normal;
  line-height: 16px;
  text-align: center;
}

/* ---------------- 主体 ---------------- */
.body {
  flex: 1;
  display: flex;
  min-height: 0;
}

/* ---------------- 主菜单 ---------------- */
.sidebar {
  flex: 0 0 var(--w-sidebar);
  border-right: 1px solid var(--c-line);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.nav {
  padding: 10px 0;
}
.nav-item {
  display: block;
  padding: 9px 22px;
  font-size: var(--fs-nav);
  color: var(--c-ink-soft);
  text-decoration: none;
  border-left: 2px solid transparent;
  transition: color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.nav-item:hover {
  color: var(--c-ink);
  background: var(--c-surface-soft);
}
.nav-item--active {
  color: var(--c-ink);
  border-left-color: var(--c-accent);
  background: var(--c-accent-soft);
}

.sb-foot {
  margin-top: auto;
  padding: 12px 16px 14px;
  border-top: 1px solid var(--c-line-soft);
}
.sb-foot-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.sb-foot-label {
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
}
.sb-foot-value {
  margin-top: 4px;
  font-size: var(--fs-small);
  color: var(--c-ink);
}
.sb-foot-note {
  margin-top: 6px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.sb-foot-mode {
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
}
.sb-foot-alert {
  margin-top: 8px;
  font-size: var(--fs-tiny);
  color: var(--c-danger);
}

/* ---------------- 主显示区 ---------------- */
.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  /* ⚠️ 必须 hidden：页面切换时"下方菜单下沉"要滑出可视区，
     没有这层裁剪它会跑到侧栏/窗口外面去，还会把布局撑高。 */
  overflow: hidden;
  position: relative;
}

/* ---------------- 消息抽屉 ---------------- */
.msg-empty p {
  margin: 0 0 8px;
}
.msg-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.msg-item {
  border: 1px solid var(--c-line);
  padding: 12px;
}
.msg-item-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.msg-item-body {
  margin: 8px 0 6px;
}
.msg-item-meta {
  font-size: var(--fs-small);
}
.msg-item-actions {
  margin-top: 10px;
  display: flex;
  gap: 14px;
}
.link-btn {
  font-size: var(--fs-small);
  color: var(--c-accent);
  text-decoration: underline;
}
.link-btn--muted {
  color: var(--c-ink-muted);
}
.link-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.msg-foot {
  font-size: var(--fs-small);
}
</style>
