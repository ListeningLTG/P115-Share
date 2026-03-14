<template>
  <div class="dashboard">
    <a-row :gutter="[16, 16]">
      <a-col :xs="24" :sm="12" :md="6">
        <a-card title="运行状态" :bordered="false" size="small">
          <a-tag color="success">运行中</a-tag>
          <a-tag v-if="version" color="orange">v{{ version }}</a-tag>
        </a-card>
      </a-col>
      <a-col :xs="24" :sm="12" :md="9">
        <a-card title="TG 机器人" :bordered="false" size="small">
          <a-tag :color="tgStatus ? 'blue' : (tgToken ? 'red' : 'default')">
            {{ tgStatus ? '已连接' : (tgToken ? '连接失败' : '未配置') }}
          </a-tag>
        </a-card>
      </a-col>
      <a-col :xs="24" :sm="12" :md="9">
        <a-card title="115 网盘账号" :bordered="false" size="small">
          <a-space wrap>
            <template v-for="acc in accounts" :key="acc.id">
              <a-tag :color="acc.is_restricted ? 'red' : (acc.is_connected ? 'blue' : 'default')">
                P{{ acc.priority }} {{ acc.name }}: {{ acc.is_restricted ? '风控中' : (acc.is_connected ? '在线' : '离线') }}
              </a-tag>
            </template>
            <a-tag v-if="accounts.length === 0" color="default">未配置</a-tag>
          </a-space>
        </a-card>
      </a-col>
    </a-row>

    <a-divider />

    <a-typography-title :level="4">快速操作</a-typography-title>

    <!-- 账号选择器 -->
    <div style="margin-bottom: 12px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
      <span style="font-size: 14px; color: #666">操作账号：</span>
      <a-select
        v-model:value="selectedAccountId"
        style="width: 200px"
        placeholder="选择账号（默认优先级最高）"
        allow-clear
      >
        <a-select-option v-for="acc in accounts" :key="acc.id" :value="acc.id">
          P{{ acc.priority }} {{ acc.name }}
          <a-tag v-if="acc.is_restricted" color="red" size="small" style="margin-left: 4px">风控</a-tag>
        </a-select-option>
      </a-select>
      <span style="font-size: 12px; color: #999">清空目录/回收站将针对所选账号执行</span>
    </div>

    <div class="action-buttons">
      <a-space wrap>
        <a-button @click="handleTestBot" type="primary" ghost>测试机器人</a-button>
        <a-button @click="handleTestChannel">测试频道</a-button>
        <a-button @click="checkStatus">刷新状态</a-button>
        <a-button @click="handleCleanupSaveDir" danger>清空保存目录</a-button>
        <a-button @click="handleCleanupRecycleBin" danger>清空回收站</a-button>
        <a-button @click="handleClearHistory" danger>清除历史记录</a-button>
      </a-space>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue';
import axios from 'axios';
import { message, Modal } from 'ant-design-vue';

interface Account {
  id: number;
  name: string;
  priority: number;
  is_connected: boolean;
  is_restricted: boolean;
}

const tgStatus = ref(false);
const tgToken = ref('');
const version = ref('');
const accounts = ref<Account[]>([]);
const selectedAccountId = ref<number | undefined>(undefined);

const checkStatus = async () => {
  try {
    const [configRes, accountsRes] = await Promise.all([
      axios.get('/api/config/'),
      axios.get('/api/accounts/'),
    ]);
    tgStatus.value = !!configRes.data.tg_bot_connected;
    tgToken.value = configRes.data.tg_bot_token || '';
    version.value = configRes.data.version || '';
    accounts.value = accountsRes.data.accounts || [];
    if (accounts.value.length > 0 && selectedAccountId.value === undefined) {
      selectedAccountId.value = accounts.value[0]!.id;
    }
    message.success('状态已刷新');
  } catch (e) {
    message.error('无法连接到后端服务器');
  }
};

const handleTestBot = async () => {
  try {
    const res = await axios.post('/api/config/test-bot');
    if (res.data.status === 'success') {
      message.success('机器人测试消息已发出');
    } else {
      message.error(res.data.message || '测试失败');
    }
  } catch (e) {
    message.error('测试请求失败');
  } finally {
    await checkStatus();
  }
};

const handleTestChannel = async () => {
  try {
    const res = await axios.post('/api/config/test-channel');
    if (res.data.status === 'success') {
      message.success('频道测试消息已发出');
    } else {
      message.error(res.data.message || '测试失败');
    }
  } catch (e) {
    message.error('测试请求失败');
  } finally {
    await checkStatus();
  }
};

const handleCleanupSaveDir = () => {
  const accName = selectedAccountId.value
    ? (accounts.value.find(a => a.id === selectedAccountId.value)?.name || '所选账号')
    : '优先级最高的账号';
  Modal.confirm({
    title: '确认清空保存目录？',
    content: `将清空 [${accName}] 的保存目录中所有文件，是否继续？`,
    okText: '确认',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      try {
        const params = selectedAccountId.value ? { account_id: selectedAccountId.value } : {};
        const res = await axios.post('/api/config/cleanup-save-dir', null, { params });
        if (res.data.status === 'success') {
          message.success('保存目录已清空');
        } else {
          message.error(res.data.message || '清空失败');
        }
      } catch (e) {
        message.error('清空请求失败');
      }
    }
  });
};

const handleCleanupRecycleBin = () => {
  const accName = selectedAccountId.value
    ? (accounts.value.find(a => a.id === selectedAccountId.value)?.name || '所选账号')
    : '优先级最高的账号';
  Modal.confirm({
    title: '确认清空回收站？',
    content: `将清空 [${accName}] 的115网盘回收站，是否继续？`,
    okText: '确认',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      try {
        const params = selectedAccountId.value ? { account_id: selectedAccountId.value } : {};
        const res = await axios.post('/api/config/cleanup-recycle-bin', null, { params });
        if (res.data.status === 'success') {
          message.success('回收站已清空');
        } else {
          message.error(res.data.message || '清空失败');
        }
      } catch (e) {
        message.error('清空请求失败');
      }
    }
  });
};

const handleClearHistory = () => {
  Modal.confirm({
    title: '确认清除历史记录？',
    content: '此操作将清空所有已缓存的分享链接记录，之后相同的链接将重新走转存流程，是否继续？',
    okText: '确认',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      try {
        const res = await axios.post('/api/config/clear-history');
        if (res.data.status === 'success') {
          message.success('历史记录已清空');
        } else {
          message.error(res.data.message || '清空失败');
        }
      } catch (e) {
        message.error('请求失败');
      }
    }
  });
};

onMounted(checkStatus);
</script>
<style scoped>
.dashboard {
  height: 100%;
  overflow-y: auto;
  padding-right: 8px;
}
</style>
