<template>
  <div style="height: 100%; overflow-y: auto; padding-right: 4px">
    <!-- 工具栏 -->
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; flex-wrap: wrap; gap: 8px">
      <div style="display: flex; align-items: center; gap: 16px; flex-wrap: wrap">
        <span style="font-size: 16px; font-weight: 500">定时分享管理</span>
      </div>
      <a-button type="primary" @click="openCreateModal">
        <template #icon><PlusOutlined /></template>
        新建定时任务
      </a-button>
    </div>

    <!-- 任务列表 -->
    <a-table
      :columns="columns"
      :data-source="tasks"
      :loading="tableLoading"
      row-key="id"
      size="small"
      :pagination="{ defaultValue: 1, pageSize: 10, showSizeChanger: true }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'name'">
          <strong>{{ record.name }}</strong>
        </template>
        
        <template v-if="column.key === 'account_id'">
          <span>{{ getAccountName(record.account_id) }}</span>
        </template>
        
        <template v-if="column.key === 'dir_path'">
          <code class="path-code">{{ record.dir_path }}</code>
        </template>

        <template v-if="column.key === 'cron_expression'">
          <a-tag color="blue">{{ record.cron_expression }}</a-tag>
        </template>
        <template v-if="column.key === 'share_mode'">
          <a-tag :color="getShareModeColor(record.share_mode)">
            {{ getShareModeText(record.share_mode) }}
          </a-tag>
        </template>
        <template v-if="column.key === 'cleanup_temp_dir'">
          <a-tag :color="record.share_mode === 'direct' ? 'default' : (record.cleanup_temp_dir ? 'red' : 'default')">
            {{ record.share_mode === 'direct' ? '不适用' : (record.cleanup_temp_dir ? '清理并清空回收站' : '保留') }}
          </a-tag>
        </template>
        <template v-if="column.key === 'min_size'">
          <a-tag :color="record.min_size > 0 ? 'purple' : 'default'">
            {{ record.min_size > 0 ? `≥ ${record.min_size} ${record.min_size_unit}` : '不检测' }}
          </a-tag>
        </template>
        <template v-if="column.key === 'target_channels'">
          <div style="max-width: 250px; display: flex; flex-wrap: wrap; gap: 4px">
            <a-tag v-for="ch in record.target_channels" :key="ch" color="cyan">
              {{ getChannelName(ch) }}
            </a-tag>
            <span v-if="!record.target_channels || record.target_channels.length === 0" style="color: #999">-</span>
          </div>
        </template>

        <template v-if="column.key === 'status'">
          <a-tag :color="getStatusColor(record.status)">
            {{ getStatusText(record.status) }}
          </a-tag>
        </template>

        <template v-if="column.key === 'enabled'">
          <a-switch
            :checked="record.enabled"
            @change="toggleTask(record)"
            size="small"
          />
        </template>

        <template v-if="column.key === 'last_run_at'">
          <span style="font-size: 12px; color: #666">{{ formatDate(record.last_run_at) }}</span>
        </template>

        <template v-if="column.key === 'actions'">
          <a-space>
            <a-button size="small" type="link" @click="openEditModal(record)">编辑</a-button>
            <a-button size="small" type="link" @click="triggerTask(record)" :loading="triggeringIds.includes(record.id)">手动执行</a-button>
            <a-popconfirm
              title="确认删除该定时分享任务？"
              ok-text="确认删除"
              ok-type="danger"
              cancel-text="取消"
              @confirm="deleteTask(record.id)"
            >
              <a-button size="small" type="link" danger>删除</a-button>
            </a-popconfirm>
          </a-space>
        </template>
      </template>
    </a-table>

    <!-- 新建/编辑对话框 -->
    <a-modal
      v-model:visible="modalVisible"
      :title="editingId ? '编辑定时分享任务' : '新建定时分享任务'"
      @ok="handleModalOk"
      :confirmLoading="modalSubmitting"
      ok-text="保存"
      cancel-text="取消"
    >
      <a-form :model="formState" :rules="formRules" ref="formRef" layout="vertical">
        <a-form-item label="任务名称" name="name">
          <a-input v-model:value="formState.name" placeholder="请输入任务描述名称，如：每日电影定时分享" />
        </a-form-item>

        <a-form-item label="执行账号" name="account_id">
          <a-select v-model:value="formState.account_id" placeholder="选择要执行网盘操作的 115 账号">
            <a-select-option v-for="acc in accounts" :key="acc.id" :value="acc.id">
              {{ acc.name }} (ID: {{ acc.id }}){{ !acc.enabled ? ' (已禁用)' : '' }}
            </a-select-option>
          </a-select>
        </a-form-item>

        <a-form-item label="网盘目录路径" name="dir_path">
          <a-input v-model:value="formState.dir_path" placeholder="网盘中的目标绝对路径，例如 /电影/欧美" />
        </a-form-item>

        <a-form-item label="Cron 定时表达式" name="cron_expression">
          <a-input v-model:value="formState.cron_expression" placeholder="格式如：0 3 * * *（每天凌晨 3:00）" />
          <div style="font-size: 11px; color: #888; margin-top: 4px">
            五域格式：分 时 日 月 周。可参考：<code>*/5 * * * *</code> (每5分钟)，<code>0 */2 * * *</code> (每2小时)
          </div>
        </a-form-item>

        <a-form-item label="文件处理模式" name="share_mode">
          <template #extra>
            <div style="font-size: 12px; color: #999; margin-top: 4px">
              <span v-if="formState.share_mode === 'move'">移动模式：原目录文件会被直接剪切移动到临时目录，原目录自动清空，速度极快且不占额外空间（适合增量分享/追更）。</span>
              <span v-else-if="formState.share_mode === 'copy'">复制模式：原目录文件会被复制一份进行分享，原目录文件保持不动，但下次会重复分享（适合全量分享/归档）。</span>
              <span v-else-if="formState.share_mode === 'direct'">直接分享模式：直接对原目录（源目录本身）进行分享，不创建临时文件夹，不移动或复制任何文件，原目录文件保持不动。</span>
            </div>
          </template>
          <a-select v-model:value="formState.share_mode">
            <a-select-option value="move">移动模式 (剪切)</a-select-option>
            <a-select-option value="copy">复制模式 (保留)</a-select-option>
            <a-select-option value="direct">直接分享模式 (只分享不操作文件)</a-select-option>
          </a-select>
        </a-form-item>

        <a-form-item
          v-if="formState.share_mode === 'move' || formState.share_mode === 'copy'"
          label="清理临时目录"
          name="cleanup_temp_dir"
        >
          <a-switch v-model:checked="formState.cleanup_temp_dir" />
          <div style="font-size: 12px; color: #999; margin-top: 4px">
            开启后，分享创建并推送成功后会删除本次临时目录，并清空该 115 账号的整个回收站；关闭则保留临时目录且不清空回收站。
          </div>
        </a-form-item>

        <a-form-item label="最小容量触发阈值" name="min_size">
          <template #extra>
            <div style="font-size: 12px; color: #999; margin-top: 4px">
              原目录内文件总容量超过该阈值时才触发分享，0 表示不检测目录容量。
            </div>
          </template>
          <a-input-group compact style="display: flex; width: 100%">
            <a-input-number
              v-model:value="formState.min_size"
              :min="0"
              :precision="2"
              style="flex: 1"
              placeholder="请输入阈值，如 1.5"
            />
            <a-select v-model:value="formState.min_size_unit" style="width: 100px">
              <a-select-option value="GB">GB</a-select-option>
              <a-select-option value="TB">TB</a-select-option>
            </a-select>
          </a-input-group>
        </a-form-item>

        <a-form-item label="推送目标频道" name="target_channels">
          <template #extra>
            <div style="font-size: 12px; color: #999; margin-top: 4px">定时任务生成分享链接后，广播推送到选中的频道。可多选。</div>
          </template>
          <a-select v-model:value="formState.target_channels" mode="multiple" placeholder="请选择推送频道">
            <a-select-option v-for="ch in availableChannels" :key="ch.id" :value="ch.id">
              {{ ch.name || ch.id }}
            </a-select-option>
          </a-select>
          <div v-if="availableChannels.length === 0" style="font-size: 12px; color: #ff4d4f; margin-top: 4px">
            提示：当前未在 [系统设置] 中配置推送频道，将无法推送。请前往配置。
          </div>
        </a-form-item>

        <a-form-item label="启用任务" name="enabled">
          <a-switch v-model:checked="formState.enabled" />
        </a-form-item>

        <a-form-item label="启用敏感词替换" name="sensitive_replace_enabled">
          <a-switch v-model:checked="formState.sensitive_replace_enabled" />
        </a-form-item>

        <a-form-item label="中文名称替换为拼音全拼" name="sensitive_replace_pinyin" v-if="formState.sensitive_replace_enabled">
          <a-select v-model:value="formState.sensitive_replace_pinyin" style="width: 280px">
            <a-select-option value="0">禁用 (不转换拼音)</a-select-option>
            <a-select-option value="1">直接替换为拼音全拼 (无条件)</a-select-option>
            <a-select-option value="2">仅在包含 TMDB ID 时替换为拼音全拼</a-select-option>
          </a-select>
        </a-form-item>

        <a-form-item label="启用 TMDB 别名替换" name="sensitive_replace_tmdb" v-if="formState.sensitive_replace_enabled">
          <a-tooltip :title="!isTmdbConfigured ? '请先在系统配置中填写 TMDB API Key' : ''">
            <span style="display: inline-block">
              <a-switch
                v-model:checked="formState.sensitive_replace_tmdb"
                :disabled="!isTmdbConfigured"
              />
            </span>
          </a-tooltip>
          <div style="font-size: 11px; color: #888; margin-top: 4px">
            开启后，若中文名称的文件或目录具有 TMDB ID 标识，使用 TMDB 英文/原版别名替换其中文名称（优先级次于敏感词映射表）。
          </div>
        </a-form-item>
      </a-form>
      
      <div v-if="(formState.share_mode === 'move' || formState.share_mode === 'copy') && formState.cleanup_temp_dir" style="margin-top: 12px">
        <a-alert message="安全警示" description="此任务会在分享成功后删除临时目录，并清空该 115 账号的整个回收站。请确保回收站中没有需要保留的文件。" type="warning" show-icon />
      </div>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue';
import axios from 'axios';
import { message } from 'ant-design-vue';
import { PlusOutlined } from '@ant-design/icons-vue';
import dayjs from 'dayjs';

interface Task {
  id: number;
  name: string;
  account_id: number;
  dir_path: string;
  cron_expression: string;
  clear_files: boolean;
  share_mode: string;
  cleanup_temp_dir: boolean;
  min_size: number;
  min_size_unit: string;
  target_channels: string[];
  enabled: boolean;
  status: string;
  last_run_at: string | null;
  created_at: string;
  sensitive_replace_enabled: boolean;
  sensitive_replace_pinyin: string | boolean;
  sensitive_replace_tmdb: boolean;
}

interface Account {
  id: number;
  name: string;
  enabled: boolean;
}

interface Channel {
  id: string;
  name: string;
  enabled: boolean;
}

const columns = [
  { title: '任务名称', key: 'name', dataIndex: 'name' },
  { title: '运行账号', key: 'account_id', dataIndex: 'account_id' },
  { title: '源目录路径', key: 'dir_path', dataIndex: 'dir_path' },
  { title: 'Cron表达式', key: 'cron_expression', dataIndex: 'cron_expression' },
  { title: '推送频道', key: 'target_channels', dataIndex: 'target_channels' },
  { title: '文件处理模式', key: 'share_mode', dataIndex: 'share_mode' },
  { title: '临时目录', key: 'cleanup_temp_dir', dataIndex: 'cleanup_temp_dir' },
  { title: '容量检测阈值', key: 'min_size', dataIndex: 'min_size' },
  { title: '运行状态', key: 'status', dataIndex: 'status' },
  { title: '启用', key: 'enabled', dataIndex: 'enabled' },
  { title: '上次运行时间', key: 'last_run_at', dataIndex: 'last_run_at' },
  { title: '操作', key: 'actions' },
];

const tasks = ref<Task[]>([]);
const accounts = ref<Account[]>([]);
const availableChannels = ref<Channel[]>([]);
const tableLoading = ref(false);
const modalVisible = ref(false);
const modalSubmitting = ref(false);
const editingId = ref<number | null>(null);
const triggeringIds = ref<number[]>([]);
const formRef = ref();

const formState = reactive({
  name: '',
  account_id: undefined as number | undefined,
  dir_path: '',
  cron_expression: '',
  clear_files: true,
  share_mode: 'move',
  cleanup_temp_dir: true,
  min_size: 0.0,
  min_size_unit: 'GB',
  target_channels: [] as string[],
  enabled: true,
  sensitive_replace_enabled: false,
  sensitive_replace_pinyin: '0',
  sensitive_replace_tmdb: false,
});

const isTmdbConfigured = ref(false);

const formRules = {
  name: [{ required: true, message: '请输入任务名称', trigger: 'blur' }],
  account_id: [{ required: true, message: '请选择执行账号', trigger: 'change' }],
  dir_path: [{ required: true, message: '请输入网盘目录路径', trigger: 'blur' }],
  cron_expression: [{ required: true, message: '请输入 Cron 表达式', trigger: 'blur' }],
};

const getShareModeColor = (mode: string) => {
  if (mode === 'move') return 'orange';
  if (mode === 'copy') return 'blue';
  if (mode === 'direct') return 'green';
  return 'default';
};

const getShareModeText = (mode: string) => {
  if (mode === 'move') return '移动';
  if (mode === 'copy') return '复制';
  if (mode === 'direct') return '直接';
  return mode;
};

const getAccountName = (id: number) => {
  const acc = accounts.value.find(a => a.id === id);
  if (!acc) return `账号 (ID: ${id})`;
  return acc.name + (!acc.enabled ? ' (已禁用)' : '');
};

const getChannelName = (id: string) => {
  const ch = availableChannels.value.find(c => c.id === id);
  return ch ? ch.name || ch.id : id;
};

const getStatusColor = (status: string) => {
  if (!status) return 'orange';
  if (status === 'success') return 'green';
  if (status.startsWith('failed')) return 'red';
  if (status === 'running') return 'blue';
  if (status === 'disabled') return 'default';
  return 'orange'; // waiting
};

const getStatusText = (status: string) => {
  if (!status) return '等待中';
  if (status === 'success') return '成功';
  if (status.startsWith('failed')) return '失败';
  if (status === 'running') return '执行中';
  if (status === 'disabled') return '已禁用';
  return '等待中';
};

const formatDate = (dateStr: string | null) => {
  if (!dateStr) return '暂未运行';
  // 后端存 UTC 无时区标记；补 Z 后由 dayjs 转成本地时间显示
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(dateStr) ? dateStr : `${dateStr}Z`;
  return dayjs(normalized).format('YYYY-MM-DD HH:mm:ss');
};

const loadTasks = async () => {
  try {
    tableLoading.value = true;
    const res = await axios.get('/api/scheduled-share/');
    tasks.value = res.data.tasks || [];
  } catch (e) {
    console.error(e);
    message.error('加载定时任务失败');
  } finally {
    tableLoading.value = false;
  }
};

const loadAccounts = async () => {
  try {
    const res = await axios.get('/api/accounts/');
    accounts.value = res.data.accounts || [];
  } catch (e) {
    console.error(e);
  }
};

const loadChannels = async () => {
  try {
    const res = await axios.get('/api/config/');
    isTmdbConfigured.value = !!res.data.tmdb_api_key;
    if (!isTmdbConfigured.value) {
      formState.sensitive_replace_tmdb = false;
    }
    if (res.data.tg_channels) {
      try {
        availableChannels.value = JSON.parse(res.data.tg_channels) || [];
      } catch (err) {
        availableChannels.value = [];
      }
    }
  } catch (e) {
    console.error(e);
  }
};

const openCreateModal = () => {
  editingId.value = null;
  formState.name = '';
  const firstAcc = accounts.value[0];
  formState.account_id = firstAcc ? firstAcc.id : undefined;
  formState.dir_path = '';
  formState.cron_expression = '0 3 * * *';
  formState.clear_files = true;
  formState.share_mode = 'move';
  formState.cleanup_temp_dir = true;
  formState.min_size = 0.0;
  formState.min_size_unit = 'GB';
  formState.target_channels = [];
  formState.enabled = true;
  formState.sensitive_replace_enabled = false;
  formState.sensitive_replace_pinyin = '0';
  formState.sensitive_replace_tmdb = false;
  modalVisible.value = true;
};

const openEditModal = (record: Task) => {
  editingId.value = record.id;
  formState.name = record.name;
  formState.account_id = record.account_id;
  formState.dir_path = record.dir_path;
  formState.cron_expression = record.cron_expression;
  formState.clear_files = record.clear_files;
  formState.share_mode = record.share_mode || (record.clear_files ? 'move' : 'copy');
  formState.cleanup_temp_dir = record.cleanup_temp_dir ?? true;
  formState.min_size = record.min_size ?? 0.0;
  formState.min_size_unit = record.min_size_unit ?? 'GB';
  formState.target_channels = [...(record.target_channels || [])];
  formState.enabled = record.enabled;
  formState.sensitive_replace_enabled = record.sensitive_replace_enabled ?? false;
  const pVal = record.sensitive_replace_pinyin;
  if (typeof pVal === 'boolean') {
    formState.sensitive_replace_pinyin = pVal ? '1' : '0';
  } else if (pVal !== undefined && pVal !== null) {
    formState.sensitive_replace_pinyin = String(pVal);
  } else {
    formState.sensitive_replace_pinyin = '0';
  }
  formState.sensitive_replace_tmdb = isTmdbConfigured.value
    ? (record.sensitive_replace_tmdb ?? false)
    : false;
  modalVisible.value = true;
};

const handleModalOk = async () => {
  try {
    await formRef.value.validate();
    modalSubmitting.value = true;
    
    const payload = { ...formState };
    if (!isTmdbConfigured.value) {
      payload.sensitive_replace_tmdb = false;
    }
    if (payload.min_size === null || payload.min_size === undefined || (payload.min_size as any) === '') {
      payload.min_size = 0.0;
    }
    if (editingId.value) {
      const res = await axios.put(`/api/scheduled-share/${editingId.value}`, payload);
      if (res.data.state) {
        message.success('更新定时任务成功');
        modalVisible.value = false;
        loadTasks();
      } else {
        message.error(res.data.message || '更新失败');
      }
    } else {
      const res = await axios.post('/api/scheduled-share/', payload);
      if (res.data.state) {
        message.success('创建定时任务成功');
        modalVisible.value = false;
        loadTasks();
      } else {
        message.error(res.data.message || '创建失败');
      }
    }
  } catch (e) {
    console.error(e);
  } finally {
    modalSubmitting.value = false;
  }
};

const toggleTask = async (record: Task) => {
  try {
    const res = await axios.post(`/api/scheduled-share/${record.id}/toggle`);
    if (res.data.state) {
      message.success(res.data.message);
      loadTasks();
    } else {
      message.error(res.data.message || '操作失败');
    }
  } catch (e) {
    console.error(e);
    message.error('切换状态失败');
  }
};

const triggerTask = async (record: Task) => {
  try {
    triggeringIds.value.push(record.id);
    const res = await axios.post(`/api/scheduled-share/${record.id}/trigger`);
    if (res.data.state) {
      message.success('任务已后台触发运行');
      loadTasks();
      // Poll task status after trigger
      setTimeout(loadTasks, 3000);
      setTimeout(loadTasks, 10000);
      setTimeout(loadTasks, 20000);
    } else {
      message.error(res.data.message || '触发失败');
    }
  } catch (e) {
    console.error(e);
    message.error('触发失败');
  } finally {
    triggeringIds.value = triggeringIds.value.filter(id => id !== record.id);
  }
};

const deleteTask = async (id: number) => {
  try {
    const res = await axios.delete(`/api/scheduled-share/${id}`);
    if (res.data.state) {
      message.success('删除成功');
      loadTasks();
    } else {
      message.error(res.data.message || '删除失败');
    }
  } catch (e) {
    console.error(e);
    message.error('删除失败');
  }
};

onMounted(() => {
  loadTasks();
  loadAccounts();
  loadChannels();
});
</script>

<style scoped>
.path-code {
  font-family: SFMono-Regular, Consolas, Liberation Mono, Menlo, monospace;
  background-color: rgba(0, 0, 0, 0.04);
  padding: .2em .4em;
  border-radius: 3px;
  font-size: 85%;
}

.dark .path-code {
  background-color: rgba(255, 255, 255, 0.08);
}
</style>
