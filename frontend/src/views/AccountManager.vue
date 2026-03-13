<template>
  <div style="height: 100%; overflow-y: auto; padding-right: 4px">
    <!-- 工具栏 -->
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; flex-wrap: wrap; gap: 8px">
      <div style="display: flex; align-items: center; gap: 16px; flex-wrap: wrap">
        <span style="font-size: 14px; color: #666">负载均衡</span>
        <a-switch
          v-model:checked="loadBalanceEnabled"
          @change="onLoadBalanceChange"
          checked-children="已开启"
          un-checked-children="已关闭"
        />
        <span style="font-size: 12px; color: #999">
          {{ loadBalanceEnabled
            ? '多账号智能调度，按队列大小和优先级自动选择，被风控账号自动跳过'
            : '按优先级依次选用账号，优先级高的被风控时自动降级到下一个' }}
        </span>
      </div>
      <a-button type="primary" @click="openCreateModal">
        <template #icon><PlusOutlined /></template>
        添加账号
      </a-button>
    </div>

    <!-- 账号列表 -->
    <a-table
      :columns="columns"
      :data-source="accounts"
      :loading="tableLoading"
      row-key="id"
      size="small"
      :pagination="false"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'name'">
          <strong>{{ record.name }}</strong>
        </template>
        <template v-if="column.key === 'status'">
          <a-space>
            <a-tag :color="record.is_connected ? 'blue' : 'default'">
              {{ record.is_connected ? '在线' : '离线' }}
            </a-tag>
            <a-tag v-if="record.is_restricted" color="red">风控中</a-tag>
          </a-space>
        </template>
        <template v-if="column.key === 'priority'">
          <a-tag>P{{ record.priority }}</a-tag>
        </template>
        <template v-if="column.key === 'enabled'">
          <a-tag :color="record.enabled ? 'green' : 'default'">{{ record.enabled ? '启用' : '禁用' }}</a-tag>
        </template>
        <template v-if="column.key === 'limits'">
          <span style="font-size: 12px; color: #666">存: {{ record.save_file_limit }} / 享: {{ record.share_file_limit }}</span>
        </template>
        <template v-if="column.key === 'actions'">
          <a-space>
            <a-button size="small" type="link" @click="openEditModal(record)">编辑</a-button>
            <a-button size="small" type="link" @click="testConnection(record)" :loading="testingId === record.id">测试</a-button>
            <a-popconfirm
              title="确认删除此账号？关联的分析结果也将一并删除。"
              ok-text="确认删除"
              ok-type="danger"
              cancel-text="取消"
              @confirm="deleteAccount(record.id)"
            >
              <a-button size="small" type="link" danger>删除</a-button>
            </a-popconfirm>
          </a-space>
        </template>
      </template>
    </a-table>

    <!-- 定时任务 & 频率控制 -->
    <a-divider />
    <a-form :model="p115Form" layout="vertical">
      <a-form-item label="清理保存目录 (Cron)" name="p115_cleanup_dir_cron">
        <a-input v-model:value="p115Form.p115_cleanup_dir_cron" placeholder="例如 */30 * * * *" />
        <div style="font-size: 12px; color: #999; margin-top: 4px">为空则不进行定时清理（全局生效，每个账号各自执行）</div>
      </a-form-item>
      <a-form-item label="清空回收站 (Cron)" name="p115_cleanup_trash_cron">
        <a-input v-model:value="p115Form.p115_cleanup_trash_cron" placeholder="例如 0 */2 * * *" />
        <div style="font-size: 12px; color: #999; margin-top: 4px">为空则不进行定时清空（全局生效，每个账号各自执行）</div>
      </a-form-item>

      <a-divider />

      <a-form-item label="容量自动清理">
        <template #extra>
          <div style="font-size: 12px; color: #999; margin-top: 4px">启用后，每半小时检测一次所有账号网盘容量，超过限制将自动清理保存目录</div>
        </template>
        <a-switch v-model:checked="p115Form.p115_cleanup_capacity_enabled" />
      </a-form-item>

      <div v-if="p115Form.p115_cleanup_capacity_enabled">
        <a-form-item label="容量检测方式">
          <a-radio-group v-model:value="p115Form.p115_cleanup_capacity_type">
            <a-radio value="ENTIRE">全网盘容量</a-radio>
            <a-radio value="DIRECTORY">保存目录容量</a-radio>
          </a-radio-group>
        </a-form-item>

        <a-form-item
          :label="p115Form.p115_cleanup_capacity_type === 'ENTIRE' ? '网盘已用空间上限' : '目录占用空间上限'"
          name="p115_cleanup_capacity_limit"
        >
          <a-row :gutter="8">
            <a-col :span="20">
              <a-input-number
                v-model:value="p115Form.p115_cleanup_capacity_limit"
                :min="0.1"
                :precision="2"
                style="width: 100%"
                placeholder="请输入容量值"
              >
                <template #addonAfter>{{ p115Form.p115_cleanup_capacity_unit }}</template>
              </a-input-number>
            </a-col>
          </a-row>
          <div style="font-size: 12px; color: #999; margin-top: 4px">
            {{ p115Form.p115_cleanup_capacity_type === 'ENTIRE' ? '当网盘已用空间超过此值时触发清理' : '当保存目录占用空间超过此值时触发清理' }}
          </div>
        </a-form-item>
      </div>

      <a-divider orientation="left">转存频率控制</a-divider>
      <a-row :gutter="16">
        <a-col :span="12">
          <a-form-item label="限制转存次数" name="p115_rate_limit_count">
            <a-input-number v-model:value="p115Form.p115_rate_limit_count" :min="0" style="width: 100%" />
            <div style="font-size: 12px; color: #999; margin-top: 4px">窗口期内允许的最大转存次数 (0 为不限制)</div>
          </a-form-item>
        </a-col>
        <a-col :span="12">
          <a-form-item label="窗口大小 (秒)" name="p115_rate_limit_window">
            <a-input-number v-model:value="p115Form.p115_rate_limit_window" :min="1" style="width: 100%" />
            <div style="font-size: 12px; color: #999; margin-top: 4px">计算转存次数的时间范围</div>
          </a-form-item>
        </a-col>
      </a-row>

      <a-row :gutter="16">
        <a-col :span="12">
          <a-form-item label="静默时长 (秒)" name="p115_rate_limit_silent_duration">
            <a-input-number v-model:value="p115Form.p115_rate_limit_silent_duration" :min="0" style="width: 100%" />
            <div style="font-size: 12px; color: #999; margin-top: 4px">触发限制后暂停转存的时长</div>
          </a-form-item>
        </a-col>
        <a-col :span="12">
          <a-form-item label="批量避让时长 (秒)" name="p115_batch_yield_duration">
            <a-input-number v-model:value="p115Form.p115_batch_yield_duration" :min="0" style="width: 100%" />
            <div style="font-size: 12px; color: #999; margin-top: 4px">TG 收到新任务后，批量任务暂停的时长</div>
          </a-form-item>
        </a-col>
      </a-row>

      <a-divider />
      <a-button type="primary" @click="saveP115Config" :loading="savingP115" block>保存 115 配置</a-button>
    </a-form>

    <!-- 创建/编辑账号弹窗 -->
    <a-modal
      v-model:open="modalVisible"
      :title="editingId ? '编辑账号' : '添加账号'"
      @ok="submitModal"
      :confirm-loading="submitting"
      ok-text="保存"
      cancel-text="取消"
      width="600px"
    >
      <a-form :model="form" layout="vertical" style="margin-top: 8px">
        <a-row :gutter="16">
          <a-col :span="14">
            <a-form-item label="账号名称" required>
              <a-input v-model:value="form.name" placeholder="例如：主账号、备用账号1" />
            </a-form-item>
          </a-col>
          <a-col :span="5">
            <a-form-item label="优先级">
              <a-input-number v-model:value="form.priority" :min="1" :max="99" style="width: 100%" />
              <div style="font-size: 11px; color: #999; margin-top: 2px">数字越小优先级越高</div>
            </a-form-item>
          </a-col>
          <a-col :span="5">
            <a-form-item label="状态">
              <a-switch v-model:checked="form.enabled" checked-children="启用" un-checked-children="禁用" />
            </a-form-item>
          </a-col>
        </a-row>

        <a-form-item label="115 Cookie" :required="!editingId">
          <a-textarea v-model:value="form.cookie" :rows="4" :placeholder="editingId ? '留空则保留原 Cookie 不变' : '请输入 115 网盘 Cookie'" />
        </a-form-item>

        <a-row :gutter="16">
          <a-col :span="16">
            <a-form-item label="保存路径">
              <a-input v-model:value="form.save_dir" placeholder="例如 115-Share" />
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item label="回收站密码">
              <a-input-password v-model:value="form.recycle_password" placeholder="留空则无密码" />
            </a-form-item>
          </a-col>
        </a-row>

        <a-row :gutter="16">
          <a-col :span="12">
            <a-form-item label="保存文件上限">
              <a-input-number v-model:value="form.save_file_limit" :min="1" style="width: 100%" />
              <div style="font-size: 11px; color: #999; margin-top: 2px">单次保存文件数量上限（默认 500）</div>
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item label="分享文件上限">
              <a-input-number v-model:value="form.share_file_limit" :min="1" style="width: 100%" />
              <div style="font-size: 11px; color: #999; margin-top: 2px">中转分享触发阈值（默认 10000）</div>
            </a-form-item>
          </a-col>
        </a-row>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, reactive } from 'vue';
import { PlusOutlined } from '@ant-design/icons-vue';
import { message } from 'ant-design-vue';
import axios from 'axios';

interface Account {
  id: number;
  name: string;
  cookie: string;
  save_dir: string;
  recycle_password: string;
  priority: number;
  enabled: boolean;
  save_file_limit: number;
  share_file_limit: number;
  is_connected: boolean;
  is_restricted: boolean;
  queue_size: number;
  last_used_at: number;
}

const accounts = ref<Account[]>([]);
const loadBalanceEnabled = ref(false);
const tableLoading = ref(false);
const testingId = ref<number | null>(null);
const modalVisible = ref(false);
const submitting = ref(false);
const editingId = ref<number | null>(null);
const savingP115 = ref(false);

const p115Form = reactive({
  p115_cleanup_dir_cron: '',
  p115_cleanup_trash_cron: '',
  p115_cleanup_capacity_enabled: false,
  p115_cleanup_capacity_limit: 0,
  p115_cleanup_capacity_unit: 'GB',
  p115_cleanup_capacity_type: 'ENTIRE',
  p115_rate_limit_count: 30,
  p115_rate_limit_window: 300,
  p115_rate_limit_silent_duration: 60,
  p115_batch_yield_duration: 10,
});

const form = reactive({
  name: '新账号',
  cookie: '',
  save_dir: '115-Share',
  recycle_password: '',
  priority: 10,
  enabled: true,
  save_file_limit: 500,
  share_file_limit: 10000,
});

const columns = [
  { title: '账号名称', key: 'name', dataIndex: 'name', width: 120 },
  { title: '优先级', key: 'priority', width: 70, align: 'center' as const },
  { title: '状态', key: 'status', width: 130 },
  { title: '启用', key: 'enabled', width: 70, align: 'center' as const },
  { title: '文件限制(存/享)', key: 'limits', width: 160 },
  { title: '保存路径', dataIndex: 'save_dir', width: 120, ellipsis: true },
  { title: '操作', key: 'actions', width: 160 },
];

const loadAccounts = async () => {
  tableLoading.value = true;
  try {
    const res = await axios.get('/api/accounts/');
    accounts.value = res.data.accounts || [];
    loadBalanceEnabled.value = res.data.load_balance_enabled || false;
  } catch (e) {
    message.error('加载账号列表失败');
  } finally {
    tableLoading.value = false;
  }
};

const loadP115Config = async () => {
  try {
    const res = await axios.get('/api/config/');
    p115Form.p115_cleanup_dir_cron = res.data.p115_cleanup_dir_cron || '';
    p115Form.p115_cleanup_trash_cron = res.data.p115_cleanup_trash_cron || '';
    p115Form.p115_cleanup_capacity_enabled = res.data.p115_cleanup_capacity_enabled || false;
    p115Form.p115_cleanup_capacity_limit = res.data.p115_cleanup_capacity_limit || 0;
    p115Form.p115_cleanup_capacity_unit = res.data.p115_cleanup_capacity_unit || 'GB';
    p115Form.p115_cleanup_capacity_type = res.data.p115_cleanup_capacity_type || 'ENTIRE';
    p115Form.p115_rate_limit_count = res.data.p115_rate_limit_count !== undefined ? res.data.p115_rate_limit_count : 30;
    p115Form.p115_rate_limit_window = res.data.p115_rate_limit_window || 300;
    p115Form.p115_rate_limit_silent_duration = res.data.p115_rate_limit_silent_duration !== undefined ? res.data.p115_rate_limit_silent_duration : 60;
    p115Form.p115_batch_yield_duration = res.data.p115_batch_yield_duration !== undefined ? res.data.p115_batch_yield_duration : 10;
  } catch (e) {
    console.error(e);
  }
};

const saveP115Config = async () => {
  savingP115.value = true;
  try {
    await axios.post('/api/config/update', { ...p115Form });
    message.success('115 配置已保存');
  } catch (e: any) {
    message.error(e.response?.data?.detail || '保存失败');
  } finally {
    savingP115.value = false;
  }
};

const onLoadBalanceChange = async (val: boolean) => {
  try {
    await axios.post('/api/accounts/load-balance', { enabled: val });
    message.success(`负载均衡已${val ? '开启' : '关闭'}`);
  } catch (e) {
    message.error('设置失败');
    loadBalanceEnabled.value = !val;
  }
};

const openCreateModal = () => {
  editingId.value = null;
  const nextNum = accounts.value.length + 1;
  Object.assign(form, {
    name: `账号${nextNum}`, cookie: '', save_dir: '115-Share',
    recycle_password: '', priority: nextNum, enabled: true,
    save_file_limit: 500, share_file_limit: 10000,
  });
  modalVisible.value = true;
};

const openEditModal = (record: Account) => {
  editingId.value = record.id;
  Object.assign(form, {
    name: record.name,
    cookie: '',  // 安全起见不回填 cookie 原文
    save_dir: record.save_dir,
    recycle_password: record.recycle_password || '',
    priority: record.priority,
    enabled: record.enabled,
    save_file_limit: record.save_file_limit,
    share_file_limit: record.share_file_limit,
  });
  modalVisible.value = true;
};

const submitModal = async () => {
  if (!form.name.trim()) { message.warning('请输入账号名称'); return; }
  if (!editingId.value && !form.cookie.trim()) { message.warning('请输入 Cookie'); return; }

  submitting.value = true;
  try {
    const payload: any = { ...form };
    // 编辑时若 cookie 为空则不更新
    if (editingId.value && !payload.cookie) delete payload.cookie;

    let savedId: number;
    if (editingId.value) {
      await axios.put(`/api/accounts/${editingId.value}`, payload);
      message.success('账号更新成功');
      savedId = editingId.value;
    } else {
      const res = await axios.post('/api/accounts/', payload);
      message.success('账号创建成功');
      savedId = res.data.data?.id ?? res.data.id;
    }
    modalVisible.value = false;
    await loadAccounts();
    // 保存后自动测试一次
    const savedAccount = accounts.value.find(a => a.id === savedId);
    if (savedAccount) {
      await testConnection(savedAccount);
    }
  } catch (e) {
    message.error('保存失败');
  } finally {
    submitting.value = false;
  }
};

const testConnection = async (record: Account) => {
  testingId.value = record.id;
  try {
    const res = await axios.post(`/api/accounts/${record.id}/test`);
    if (res.data.state) {
      message.success(res.data.message);
    } else {
      message.error(res.data.message);
    }
    await loadAccounts();
  } catch (e) {
    message.error('测试请求失败');
  } finally {
    testingId.value = null;
  }
};

const deleteAccount = async (id: number) => {
  try {
    await axios.delete(`/api/accounts/${id}`);
    message.success('账号已删除');
    await loadAccounts();
  } catch (e) {
    message.error('删除失败');
  }
};

onMounted(() => {
  loadAccounts();
  loadP115Config();
});

// 暴露给父组件
defineExpose({ loadAccounts, accounts, loadBalanceEnabled });
</script>
