<template>
  <a-layout style="height: 100vh; overflow: hidden">
    <!-- Desktop Sider -->
    <a-layout-sider 
      v-if="!isMobile"
      v-model:collapsed="collapsed" 
      collapsible
      style="overflow: auto; height: 100vh; position: fixed; left: 0; top: 0; bottom: 0; z-index: 100"
    >
      <div class="logo">
        <img src="/logo.png" alt="logo" style="height: 24px; margin-right: 8px" />
        <span v-if="!collapsed">115网盘转存分享</span>
      </div>
      <a-menu v-model:selectedKeys="selectedKeys" theme="dark" mode="inline">
        <a-menu-item key="dashboard" @click="currentView = 'dashboard'">
          <template #icon><DashboardOutlined /></template>
          <span>控制台</span>
        </a-menu-item>
        <a-menu-item key="logs" @click="currentView = 'logs'">
          <template #icon><ContainerOutlined /></template>
          <span>实时日志</span>
        </a-menu-item>
        <a-menu-item key="excel" @click="currentView = 'excel'">
          <template #icon><FileExcelOutlined /></template>
          <span>批量转存分享</span>
        </a-menu-item>
        <a-menu-item key="share" @click="currentView = 'share'">
          <template #icon><ShareAltOutlined /></template>
          <span>分享链接管理</span>
        </a-menu-item>
        <a-menu-item key="accounts" @click="currentView = 'accounts'">
          <template #icon><CloudServerOutlined /></template>
          <span>账号管理</span>
        </a-menu-item>
        <!-- 敏感词库功能开发中，暂时隐藏 -->
        <a-menu-item key="settings" @click="currentView = 'settings'">
          <template #icon><SettingOutlined /></template>
          <span>系统配置</span>
        </a-menu-item>
      </a-menu>
    </a-layout-sider>

    <!-- Mobile Drawer -->
    <a-drawer
      v-else
      v-model:open="drawerVisible"
      placement="left"
      :closable="false"
      :body-style="{ padding: 0, height: '100vh', background: '#001529' }"
      width="200px"
    >
      <div class="logo">
        <img src="/logo.png" alt="logo" style="height: 24px; margin-right: 8px" />
        <span>115网盘转存分享</span>
      </div>
      <a-menu v-model:selectedKeys="selectedKeys" theme="dark" mode="inline" @click="drawerVisible = false">
        <a-menu-item key="dashboard" @click="currentView = 'dashboard'">
          <template #icon><DashboardOutlined /></template>
          <span>控制台</span>
        </a-menu-item>
        <a-menu-item key="logs" @click="currentView = 'logs'">
          <template #icon><ContainerOutlined /></template>
          <span>实时日志</span>
        </a-menu-item>
        <a-menu-item key="excel" @click="currentView = 'excel'">
          <template #icon><FileExcelOutlined /></template>
          <span>批量转存分享</span>
        </a-menu-item>
        <a-menu-item key="share" @click="currentView = 'share'">
          <template #icon><ShareAltOutlined /></template>
          <span>分享链接管理</span>
        </a-menu-item>
        <a-menu-item key="accounts" @click="currentView = 'accounts'">
          <template #icon><CloudServerOutlined /></template>
          <span>账号管理</span>
        </a-menu-item>
        <!-- 敏感词库功能开发中，暂时隐藏 -->
        <a-menu-item key="settings" @click="currentView = 'settings'">
          <template #icon><SettingOutlined /></template>
          <span>系统配置</span>
        </a-menu-item>
      </a-menu>
    </a-drawer>

    <a-layout :style="{ ...layoutStyle, height: '100vh', display: 'flex', flexDirection: 'column' }">
      <a-layout-header :style="{ background: antdToken.colorBgContainer, padding: '0 16px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', position: 'sticky', top: 0, zIndex: 99, width: '100%', boxShadow: '0 2px 8px ' + antdToken.colorFillSecondary }">
        <div style="display: flex; align-items: center">
          <a-button 
            v-if="isMobile" 
            type="text" 
            @click="drawerVisible = true" 
            style="margin-right: 16px; font-size: 18px"
          >
            <MenuOutlined />
          </a-button>
          <img src="/logo.png" alt="logo" style="height: 28px; margin-right: 12px" />
          <h2 style="margin: 0; font-size: 18px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis">
            {{ isMobile ? '115网盘转存' : '115网盘转存分享-管理面板' }}
          </h2>
        </div>
        
        <div style="display: flex; align-items: center; gap: 16px">
          <!-- Theme Toggle -->
          <a-button type="text" @click="themeStore.toggleTheme" style="font-size: 18px; display: flex; align-items: center; justify-content: center">
            <template #icon>
              <BulbOutlined :style="{ color: themeStore.mode === 'dark' ? '#ffcc00' : 'inherit' }" />
            </template>
          </a-button>

          <!-- User Profile -->
        <a-dropdown>
          <div class="user-avatar-wrap" @click.prevent>
            <a-avatar :src="auth.user?.avatar_url || '/logo.png'" />
            <span v-if="!isMobile" class="user-name">{{ auth.user?.username }}</span>
          </div>
          <template #overlay>
            <a-menu>
              <a-menu-item key="profile" @click="showProfileModal = true">
                <template #icon><UserOutlined /></template>
                个人中心
              </a-menu-item>
              <a-menu-divider />
              <a-menu-item key="logout" @click="handleLogout" danger>
                <template #icon><LogoutOutlined /></template>
                退出登录
              </a-menu-item>
            </a-menu>
          </template>
        </a-dropdown>
      </div>
    </a-layout-header>
      
      <a-layout-content style="flex: 1; overflow: hidden; display: flex; flex-direction: column; margin: 16px">
        <div :style="{ 
          padding: isMobile ? '16px' : '24px', 
          background: antdToken.colorBgContainer, 
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          borderRadius: '4px',
          overflow: 'hidden'
        }">
          <Dashboard v-if="currentView === 'dashboard'" />
          <ExcelBatch v-if="currentView === 'excel'" />
          <ShareManager v-if="currentView === 'share'" />
          <LogViewer v-if="currentView === 'logs'" />
          <AccountManager v-if="currentView === 'accounts'" />
          <Sensitive v-if="currentView === 'sensitive'" />
          <Settings v-if="currentView === 'settings'" />
        </div>
      </a-layout-content>
    </a-layout>

    <!-- Profile Modal -->
    <a-modal v-model:open="showProfileModal" title="个人中心" @ok="handleProfileUpdate" :confirmLoading="profileLoading">
      <a-form layout="vertical">
        <a-form-item label="用户名">
          <a-input :value="auth.user?.username" disabled />
        </a-form-item>
        <a-form-item label="新密码">
          <a-input-password v-model:value="profileForm.password" placeholder="留空则不修改" />
        </a-form-item>
        <a-form-item label="修改头像">
          <a-upload
            name="file"
            list-type="picture-card"
            class="avatar-uploader"
            :show-upload-list="false"
            action="/api/auth/upload_avatar"
            :headers="{ Authorization: `Bearer ${auth.token}` }"
            @change="handleUploadChange"
          >
            <img v-if="profileForm.avatar_url" :src="profileForm.avatar_url" alt="avatar" style="width: 100%; height: 100%; object-fit: cover" />
            <div v-else>
              <plus-outlined v-if="!uploading" />
              <loading-outlined v-else />
              <div class="ant-upload-text">点击上传</div>
            </div>
          </a-upload>
          <div style="margin-top: 8px; color: #999; font-size: 12px">支持 JPG/PNG 格式，图片将保存至服务器。</div>
        </a-form-item>
      </a-form>
    </a-modal>
  </a-layout>
</template>

<script setup lang="ts">
import { ref, computed, watch, reactive, onMounted } from 'vue';
import {
  DashboardOutlined,
  SettingOutlined,
  ContainerOutlined,
  MenuOutlined,
  UserOutlined,
  LogoutOutlined,
  PlusOutlined,
  LoadingOutlined,
  BulbOutlined,
  FileExcelOutlined,
  ShareAltOutlined,
  CloudServerOutlined,
  WarningOutlined
} from '@ant-design/icons-vue';
import { Grid, message, theme } from 'ant-design-vue';
import Dashboard from './Dashboard.vue';
import ExcelBatch from './ExcelBatch.vue';
import ShareManager from './ShareManager.vue';
import LogViewer from './LogViewer.vue';
import Settings from './Settings.vue';
import AccountManager from './AccountManager.vue';
import Sensitive from './Sensitive.vue';
import { useAuthStore } from '../stores/auth';
import { useThemeStore } from '../stores/theme';
import { useRouter } from 'vue-router';
import axios from 'axios';

const { useToken } = theme;
const { token: antdToken } = useToken();

const auth = useAuthStore();
const themeStore = useThemeStore();
const router = useRouter();
const useBreakpoint = Grid.useBreakpoint;
const screens = useBreakpoint();

const isMobile = computed(() => !screens.value.md && (screens.value.sm || screens.value.xs));
const drawerVisible = ref(false);
const collapsed = ref<boolean>(false);
const selectedKeys = ref<string[]>(['dashboard']);
const currentView = ref<string>('dashboard');

// Profile logic
const showProfileModal = ref(false);
const profileLoading = ref(false);
const uploading = ref(false);
const profileForm = reactive({
  password: '',
  avatar_url: auth.user?.avatar_url || '/logo.png'
});

// 监听模态框打开，同步最新的头像URL
watch(showProfileModal, (isOpen) => {
  if (isOpen) {
    profileForm.avatar_url = auth.user?.avatar_url || '/logo.png';
    profileForm.password = '';
  }
});

const handleUploadChange = (info: any) => {
  if (info.file.status === 'uploading') {
    uploading.value = true;
    return;
  }
  if (info.file.status === 'done') {
    uploading.value = false;
    message.success('头像预览成功，点击确定保存');
    profileForm.avatar_url = info.file.response.avatar_url;
  } else if (info.file.status === 'error') {
    uploading.value = false;
    message.error('上传失败');
  }
};

const handleProfileUpdate = async () => {
  profileLoading.value = true;
  try {
    const data: any = {};
    if (profileForm.password) data.password = profileForm.password;
    if (profileForm.avatar_url) data.avatar_url = profileForm.avatar_url;
    
    await axios.put('/api/auth/profile', data);
    message.success('更新成功');
    if (data.password) {
      message.info('密码已修改，请重新登录');
      handleLogout();
    } else {
      await auth.fetchProfile();
      showProfileModal.value = false;
    }
  } catch (error) {
    message.error('更新失败');
  } finally {
    profileLoading.value = false;
  }
};

const handleLogout = () => {
  auth.logout();
  router.push('/login');
};

// 自动根据屏幕尺寸收缩菜单
watch(
  () => screens.value.lg,
  (isLg) => {
    if (!isLg && !isMobile.value) {
      collapsed.value = true;
    } else if (isLg) {
      collapsed.value = false;
    }
  },
  { immediate: true }
);

const layoutStyle = computed(() => {
  if (isMobile.value) {
    return { marginLeft: 0, transition: 'all 0.2s' };
  }
  return { marginLeft: collapsed.value ? '80px' : '200px', transition: 'all 0.2s' };
});

onMounted(() => {
  if (!auth.user) {
    auth.fetchProfile();
  }
});
</script>

<style scoped>
.logo {
  height: 32px;
  margin: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  line-height: 32px;
  font-weight: bold;
}

.user-avatar-wrap {
  display: flex;
  align-items: center;
  cursor: pointer;
  padding: 0 8px;
  border-radius: 4px;
  transition: all 0.3s;
}

.user-avatar-wrap:hover {
  background: rgba(0, 0, 0, 0.05);
}

.user-name {
  margin-left: 8px;
  max-width: 100px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 576px) {
  h2 {
    font-size: 16px !important;
  }
}

.avatar-uploader :deep(.ant-upload) {
  width: 128px;
  height: 128px;
}

.ant-upload-select-picture-card i {
  font-size: 32px;
  color: #999;
}

.ant-upload-select-picture-card .ant-upload-text {
  margin-top: 8px;
  color: #666;
}
</style>
