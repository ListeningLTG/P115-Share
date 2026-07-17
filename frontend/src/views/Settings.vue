<template>
  <div class="settings">
    <a-form 
      ref="formRef"
      :model="formState" 
      :rules="rules"
      layout="vertical" 
      @finish="onFinish" 
      size="middle"
    >
      <a-typography-title :level="5" style="margin-bottom: 24px">系统配置</a-typography-title>
      
      <a-collapse ghost default-active-key="tg">
        <a-collapse-panel key="tg" header="Telegram 配置">
          <a-form-item label="Bot Token" name="tg_bot_token">
            <a-input-password v-model:value="formState.tg_bot_token" placeholder="请输入 TG Bot Token" />
          </a-form-item>
          
          <a-divider orientation="left">推送频道列表</a-divider>
          
          <div v-for="(channel, index) in tgChannels" :key="index" class="channel-item">
            <a-row :gutter="12" align="middle">
              <a-col :flex="'180px'">
                <a-input v-model:value="channel.id" placeholder="ID: @name / -100..." />
              </a-col>
              <a-col :flex="'240px'">
                <a-input-group compact style="display: flex">
                  <a-input v-model:value="channel.name" disabled placeholder="频道名称" style="flex: 1" />
                  <a-button @click="getChannelInfo(index)" :loading="channel.loading">获取</a-button>
                </a-input-group>
              </a-col>
              <a-col :flex="'auto'">
                <a-space :size="12">
                  <span class="switch-item">
                    <span class="switch-label">启用</span>
                    <a-switch v-model:checked="channel.enabled" size="small" />
                  </span>
                  <span class="switch-item">
                    <span class="switch-label">简洁</span>
                    <a-switch v-model:checked="channel.concise" size="small" />
                  </span>
                  <span class="switch-item">
                    <span class="switch-label">自动转发</span>
                    <a-switch v-model:checked="channel.auto_forward" size="small" />
                  </span>
                  <span class="switch-item">
                    <span class="switch-label">移除图片</span>
                    <a-switch v-model:checked="channel.remove_image" :disabled="channel.concise" size="small" />
                  </span>
                  <span class="switch-item">
                    <span class="switch-label">超链接平铺</span>
                    <a-switch v-model:checked="channel.flatten_link" :disabled="channel.concise" size="small" />
                  </span>
                </a-space>
              </a-col>
              <a-col :flex="'60px'" style="text-align: right">
                <a-button type="link" danger @click="removeChannel(index)" style="padding: 0">
                  <template #icon><DeleteOutlined /></template>
                </a-button>
              </a-col>
            </a-row>
          </div>
          
            <a-button type="dashed" block @click="addChannel" style="margin-bottom: 24px">
              <template #icon><PlusOutlined /></template>
              添加推送频道
            </a-button>

          <a-form-item label="User ID" name="tg_user_id">
            <a-input v-model:value="formState.tg_user_id" placeholder="接收保存成功通知的用户 ID" />
          </a-form-item>
          <a-form-item label="Chat ID 白名单" name="tg_allow_chats">
            <a-input v-model:value="formState.tg_allow_chats" placeholder="允许使用机器人的 ID (多个用逗号分隔)" />
          </a-form-item>

          <a-form-item label="审核超时时间 (小时)" name="tg_poll_timeout_hours">
            <template #extra>
              <div style="font-size: 12px; color: #999; margin-top: 4px">链接审核轮询的最大等待时长，超过后将停止轮询并提示手动检查</div>
            </template>
            <a-input-number 
              v-model:value="formState.tg_poll_timeout_hours" 
              :min="1" 
              :max="72" 
              :step="1"
              style="width: 100%"
            />
          </a-form-item>

          <!-- <a-form-item label="跳过大包 (500文件限制)">
            <template #extra>
              <div style="font-size: 12px; color: #999; margin-top: 4px">开启后，机器人收到的链接如果是大包将直接跳过并提醒，不进行分批转存</div>
            </template>
            <a-switch v-model:checked="formState.tg_skip_large_package" />
          </a-form-item> -->
          
          <a-divider />
          <a-button type="primary" @click="onFinish('tg')" :loading="loading" block>保存 Telegram 配置</a-button>
        </a-collapse-panel>

        <a-collapse-panel key="proxy" header="代理配置">
          <a-form-item label="启用代理" style="margin-bottom: 16px">
            <a-switch v-model:checked="formState.proxy_enabled" />
          </a-form-item>
          
          <div :style="{ opacity: formState.proxy_enabled ? 1 : 0.5, transition: 'all 0.3s', pointerEvents: formState.proxy_enabled ? 'auto' : 'none' }">
            <a-row :gutter="16">
              <a-col :span="16">
                <a-form-item label="代理地址" name="proxy_host">
                  <a-input v-model:value="formState.proxy_host" placeholder="例如 192.168.100.218 或 127.0.0.1" />
                </a-form-item>
              </a-col>
              <a-col :span="8">
                <a-form-item label="代理端口" name="proxy_port">
                  <a-input v-model:value="formState.proxy_port" placeholder="例如 7890" />
                </a-form-item>
              </a-col>
            </a-row>

            <a-row :gutter="16">
              <a-col :span="12">
                <a-form-item label="用户名 (可选)" name="proxy_user">
                  <a-input v-model:value="formState.proxy_user" placeholder="代理用户名" />
                </a-form-item>
              </a-col>
              <a-col :span="12">
                <a-form-item label="密码 (可选)" name="proxy_pass">
                  <a-input-password v-model:value="formState.proxy_pass" placeholder="代理密码" />
                </a-form-item>
              </a-col>
            </a-row>

            <a-row :gutter="16">
              <a-col :span="14">
                <a-form-item label="协议类型" name="proxy_type" style="margin-bottom: 0">
                  <a-select v-model:value="formState.proxy_type">
                    <a-select-option value="HTTP">HTTP</a-select-option>
                    <a-select-option value="SOCKS5">SOCKS5</a-select-option>
                  </a-select>
                </a-form-item>
              </a-col>
              <a-col :span="10">
                <div style="height: 32px"></div> <!-- 占位符，对齐 label 空间 -->
                <a-button @click="detectProtocol" :loading="detecting" block shadow="false">
                  <template #icon><SearchOutlined /></template>
                  自动检测协议
                </a-button>
              </a-col>
            </a-row>

            <div style="margin-top: 24px; display: flex; gap: 8px">
              <a-button @click="testProxy" :loading="testingProxy">测试代理连接</a-button>
            </div>
          </div>
          
          <a-divider />
          <a-button type="primary" @click="onFinish('proxy')" :loading="loading" block>保存代理配置</a-button>
        </a-collapse-panel>

        <a-collapse-panel key="save" header="直接保存 & 默认行为">
          <a-form-item label="默认指令模式" name="tg_default_command_mode">
            <template #extra>
              <div style="font-size: 12px; color: #999; margin-top: 4px">TG 机器人收到链接且无 /save 或 /share 指令时的全局默认处理模式。</div>
            </template>
            <a-select v-model:value="formState.tg_default_command_mode">
              <a-select-option value="share">分享并推送 (share)</a-select-option>
              <a-select-option value="save">直接保存不推送 (save)</a-select-option>
            </a-select>
          </a-form-item>

          <a-form-item label="直接保存账号" name="direct_save_account_id">
            <template #extra>
              <div style="font-size: 12px; color: #999; margin-top: 4px">使用 /save 指令时，将资源保存至哪个 115 账号。</div>
            </template>
            <a-select v-model:value="formState.direct_save_account_id">
              <a-select-option :value="0">默认/首选账号</a-select-option>
              <a-select-option v-for="acc in accounts" :key="acc.id" :value="acc.id">
                {{ acc.name }} (ID: {{ acc.id }})
              </a-select-option>
            </a-select>
          </a-form-item>

          <a-form-item label="直接保存目录" name="direct_save_dir">
            <template #extra>
              <div style="font-size: 12px; color: #999; margin-top: 4px">直接转存时在 115 网盘中存放的相对目标路径。</div>
            </template>
            <a-input v-model:value="formState.direct_save_dir" placeholder="例如 115-Save" />
          </a-form-item>

          <a-divider />
          <a-button type="primary" @click="onFinish('save')" :loading="loading" block>保存保存与行为配置</a-button>
        </a-collapse-panel>

        <a-collapse-panel key="tmdb" header="TMDB 配置">
          <a-form-item label="TMDB API Key" name="tmdb_api_key">
            <template #extra>
              <div style="font-size: 12px; color: #999; margin-top: 4px">用于解析影视剧名的 TMDB ID 并自动进行英文/原版别名替换。</div>
            </template>
            <a-input-password v-model:value="formState.tmdb_api_key" placeholder="请输入 TMDB API Key" />
          </a-form-item>

          <a-divider />
          <a-button type="primary" @click="onFinish('tmdb')" :loading="loading" block>保存 TMDB 配置</a-button>
        </a-collapse-panel>
      </a-collapse>
    </a-form>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, onMounted, computed } from 'vue';
import axios from 'axios';
import { message } from 'ant-design-vue';
import { SearchOutlined, PlusOutlined, DeleteOutlined } from '@ant-design/icons-vue';

const loading = ref(false);
const testingProxy = ref(false);
const detecting = ref(false);
const formRef = ref();

interface ChannelConfig {
  id: string;
  enabled: boolean;
  concise: boolean;
  auto_forward: boolean;
  remove_image: boolean;
  flatten_link: boolean;
  name?: string;
  loading?: boolean;
}

const tgChannels = ref<ChannelConfig[]>([]);

interface Account {
  id: number;
  name: string;
  enabled: boolean;
}
const accounts = ref<Account[]>([]);

const formState = reactive({
  tg_bot_token: '',
  tg_user_id: '',
  tg_allow_chats: '',
  tg_skip_large_package: false,
  tg_poll_timeout_hours: 6,
  proxy_enabled: false,
  proxy_host: '',
  proxy_port: '',
  proxy_user: '',
  proxy_pass: '',
  proxy_type: 'HTTP',
  direct_save_account_id: 0,
  direct_save_dir: '115-Save',
  tg_default_command_mode: 'share',
  tmdb_api_key: '',
});

const loadAccounts = async () => {
  try {
    const res = await axios.get('/api/accounts/');
    accounts.value = res.data.accounts || [];
  } catch (e) {
    console.error("加载账号列表失败:", e);
  }
};

const addChannel = () => {
  tgChannels.value.push({ id: '', enabled: true, concise: false, auto_forward: true, remove_image: false, flatten_link: false, name: '' });
};

const removeChannel = (index: number) => {
  tgChannels.value.splice(index, 1);
};

const validateProxyHost = (_rule: any, value: string) => {
  if (formState.proxy_enabled && !value) {
    return Promise.reject('启用代理时，代理地址不能为空');
  }
  return Promise.resolve();
};

const validateProxyPort = (_rule: any, value: string) => {
  if (formState.proxy_enabled && !value) {
    return Promise.reject('启用代理时，代理端口不能为空');
  }
  return Promise.resolve();
};

const rules = computed(() => ({
  tg_bot_token: [{ required: true, message: '请输入 Bot Token', trigger: 'blur' }],
  tg_user_id: [{ required: true, message: '请输入 User ID', trigger: 'blur' }],
  tg_allow_chats: [{ required: true, message: '请输入 Chat ID 白名单', trigger: 'blur' }],
  proxy_host: [{ validator: validateProxyHost, trigger: 'change' }],
  proxy_port: [{ validator: validateProxyPort, trigger: 'change' }],
  tmdb_api_key: []
}));

const loadConfig = async () => {
  try {
    const res = await axios.get('/api/config/');
    formState.tg_bot_token = res.data.tg_bot_token || '';
    formState.tg_user_id = res.data.tg_user_id || '';
    formState.tg_allow_chats = res.data.tg_allow_chats || '';
    formState.tg_skip_large_package = res.data.tg_skip_large_package || false;
    formState.tg_poll_timeout_hours = res.data.tg_poll_timeout_hours !== undefined ? res.data.tg_poll_timeout_hours : 6;
    
    // Handle tg_channels JSON
    if (res.data.tg_channels) {
      try {
        const channels = JSON.parse(res.data.tg_channels);
        // Ensure defaults for existing channels
        tgChannels.value = channels.map((c: any) => ({
          ...c,
          enabled: c.enabled !== undefined ? c.enabled : true,
          concise: c.concise !== undefined ? c.concise : false,
          auto_forward: c.auto_forward !== undefined ? c.auto_forward : true,
          remove_image: c.remove_image !== undefined ? c.remove_image : false,
          flatten_link: c.flatten_link !== undefined ? c.flatten_link : false
        }));
      } catch (e) {
        console.error("Failed to parse tg_channels:", e);
        tgChannels.value = [];
      }
    } else if (res.data.tg_channel_id) {
      // Compatibility for old single channel
      tgChannels.value = [{ id: res.data.tg_channel_id, enabled: true, concise: false, auto_forward: true, remove_image: false, flatten_link: false }];
    }
    
    formState.proxy_enabled = res.data.proxy_enabled || false;
    formState.proxy_host = res.data.proxy_host || '';
    formState.proxy_port = res.data.proxy_port || '';
    formState.proxy_user = res.data.proxy_user || '';
    formState.proxy_pass = res.data.proxy_pass || '';
    formState.proxy_type = res.data.proxy_type || 'HTTP';
    formState.direct_save_account_id = res.data.direct_save_account_id !== undefined ? res.data.direct_save_account_id : 0;
    formState.direct_save_dir = res.data.direct_save_dir || '115-Save';
    formState.tg_default_command_mode = res.data.tg_default_command_mode || 'share';
    formState.tmdb_api_key = res.data.tmdb_api_key || '';
  } catch (e) {
    console.error(e);
  }
};

const onFinish = async (section: 'tg' | 'proxy' | 'save' | 'tmdb' = 'tg') => {
  try {
    const sectionFields: Record<string, string[]> = {
      tg: ['tg_bot_token', 'tg_user_id', 'tg_allow_chats', 'tg_skip_large_package', 'tg_poll_timeout_hours'],
      proxy: ['proxy_enabled', 'proxy_host', 'proxy_port', 'proxy_user', 'proxy_pass', 'proxy_type'],
      save: ['direct_save_account_id', 'direct_save_dir', 'tg_default_command_mode'],
      tmdb: ['tmdb_api_key']
    };

    await formRef.value.validate(sectionFields[section]!);
    
    loading.value = true;
    const payload: Record<string, any> = {};
    sectionFields[section]!.forEach(field => {
      payload[field] = (formState as any)[field];
    });
    
    if (section === 'tg') {
      // Validate that all channels have an ID
      const hasEmptyId = tgChannels.value.some(c => !c.id.trim());
      if (hasEmptyId) {
        message.warning('请先填写 Channel ID，或删除不需要的频道');
        return;
      }

      // Filter out empty channel IDs and stringify
      const filteredChannels = tgChannels.value.filter(c => c.id.trim() !== '');
      payload.tg_channels = JSON.stringify(filteredChannels);
      // We still update tg_channel_id for robustness if it's there
      if (filteredChannels.length > 0) {
        payload.tg_channel_id = (filteredChannels[0] as ChannelConfig).id;
      }
    }

    const res = await axios.post('/api/config/update', payload);
    message.success(
      section === 'tg' ? 'Telegram 配置已保存' :
      section === 'proxy' ? '代理配置已保存' :
      section === 'tmdb' ? 'TMDB 配置已保存' : '保存与行为配置已保存'
    );
    if (res.data.bot_restarted) {
      message.info('机器人已根据新配置安全重启');
    }
  } catch (e: any) {
    if (e.errorFields) {
      message.error('请检查表单填写是否正确');
    } else {
      console.error(e);
      message.error(e.response?.data?.detail || '保存失败');
    }
  } finally {
    loading.value = false;
  }
};

const testProxy = async () => {
  try {
    testingProxy.value = true;
    const res = await axios.post('/api/config/test-proxy', formState);
    if (res.data.status === 'success') {
      message.success(res.data.message);
    } else {
      message.error(res.data.message);
    }
  } catch (e: any) {
    message.error(e.response?.data?.detail || '测试失败');
  } finally {
    testingProxy.value = false;
  }
};

const getChannelInfo = async (index: number) => {
  const channel = tgChannels.value[index];
  if (!channel) return;
  
  if (!channel.id) {
    message.warning('请先输入频道 ID');
    return;
  }
  
  try {
    channel.loading = true;
    const res = await axios.post('/api/config/get-telegram-chat-name', { chat_id: channel.id });
    if (res.data.status === 'success') {
      channel.name = res.data.data.title;
      message.success(`已获取频道名称: ${channel.name}`);
    } else {
      message.error(res.data.message);
    }
  } catch (e: any) {
    message.error(e.response?.data?.message || '获取频道信息失败');
  } finally {
    channel.loading = false;
  }
};

const detectProtocol = async () => {
  if (!formState.proxy_host || !formState.proxy_port) {
    message.warning('请先填写地址和端口');
    return;
  }
  try {
    detecting.value = true;
    const res = await axios.post('/api/config/detect-proxy-protocol', formState);
    if (res.data.status === 'success') {
      formState.proxy_type = res.data.protocol;
      message.success(res.data.protocol);
    } else {
      message.error(res.data.message);
    }
  } catch (e: any) {
    message.error(e.response?.data?.detail || '检测失败');
  } finally {
    detecting.value = false;
  }
};

onMounted(() => {
  loadConfig();
  loadAccounts();
});
</script>

<style scoped>
.settings {
  height: 100%;
  overflow-y: auto;
  padding-right: 8px;
}

.channel-item {
  background: #fafafa;
  padding: 12px;
  border-radius: 8px;
  margin-bottom: 12px;
  border: 1px solid #f0f0f0;
  transition: all 0.3s;
}

.switch-item {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.switch-label {
  font-size: 13px;
  color: rgba(0, 0, 0, 0.45);
}

.channel-item:hover {
  border-color: #40a9ff;
}
</style>

<style>
.dark .channel-item {
  background: #1f1f1f !important;
  border: 1px solid #303030 !important;
}

.dark .switch-label {
  color: rgba(255, 255, 255, 0.45) !important;
}
</style>
