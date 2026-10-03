cmake_minimum_required(VERSION 3.20)
project(stoke-qt VERSION 0.3.0)

find_package(ECM 6.0 REQUIRED NO_MODULE)
set(CMAKE_MODULE_PATH ${ECM_MODULE_PATH})
include(KDEInstallDirs)
include(KDECMakeSettings)
include(KDECompilerSettings NO_POLICY_SCOPE)

find_package(Qt6 6.6 REQUIRED COMPONENTS Core Gui Widgets Quick QuickControls2 QuickDialogs2)
find_package(KF6 6.0 REQUIRED COMPONENTS I18n Kirigami)

add_subdirectory(src)

# The .po files live at stoke-qt/po, a sibling of this directory, so that
# a checkout can be pointed at with -S app the way the PKGBUILD does. Turns
# each po/<lang>/stoke-qt.po into share/locale/<lang>/LC_MESSAGES/stoke-qt.mo.
ki18n_install(../po)
