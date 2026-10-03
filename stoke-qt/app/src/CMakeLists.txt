add_executable(stoke-qt main.cpp backend.cpp resources.qrc)

target_link_libraries(stoke-qt PRIVATE
    Qt6::Core Qt6::Gui Qt6::Widgets Qt6::Quick Qt6::QuickControls2 Qt6::QuickDialogs2
    KF6::I18n KF6::I18nQml
)

install(TARGETS stoke-qt ${KDE_INSTALL_TARGETS_DEFAULT_ARGS})
