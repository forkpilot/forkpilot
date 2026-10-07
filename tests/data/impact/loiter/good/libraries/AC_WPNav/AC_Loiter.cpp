




extern const AP_HAL::HAL& hal;











const AP_Param::GroupInfo AC_Loiter::var_info[] = {

    // @Param: ANG_MAX
    // @DisplayName: Loiter pilot angle max
    // @Description{Copter, Sub}: Loiter maximum pilot requested lean angle. Set to zero for 2/3 of PSC_ANGLE_MAX/ANGLE_MAX. The maximum vehicle lean angle is still limited by PSC_ANGLE_MAX/ANGLE_MAX
    // @Description: Loiter maximum pilot requested lean angle. Set to zero for 2/3 of Q_P_ANGLE_MAX/Q_ANGLE_MAX. The maximum vehicle lean angle is still limited by Q_P_ANGLE_MAX/Q_ANGLE_MAX
    // @Units: deg
    // @Range: 0 45
    // @Increment: 1
    // @User: Advanced
    AP_GROUPINFO("ANG_MAX",  1, AC_Loiter, _angle_max_deg, 0.0f),

    // @Param: SPEED
    // @DisplayName: Loiter Horizontal Maximum Speed
    // @Description: Defines the maximum speed in cm/s which the aircraft will travel horizontally while in loiter mode
    // @Units: cm/s
    // @Range: 20 3500
    // @Increment: 50
    // @User: Standard
    AP_GROUPINFO("SPEED", 2, AC_Loiter, _speed_max_ne_cms, LOITER_SPEED_DEFAULT_CM),

    // @Param: ACC_MAX
    // @DisplayName: Loiter maximum correction acceleration
    // @Description: Loiter maximum correction acceleration in cm/s/s.  Higher values cause the copter to correct position errors more aggressively.
    // @Units: cm/s/s
    // @Range: 100 981
    // @Increment: 1
    // @User: Advanced
    AP_GROUPINFO("ACC_MAX", 3, AC_Loiter, _accel_max_ne_cmss, LOITER_ACCEL_MAX_DEFAULT_CMSS),

    // @Param: BRK_ACCEL
    // @DisplayName: Loiter braking acceleration
    // @Description: Loiter braking acceleration in cm/s/s. Higher values stop the copter more quickly when the stick is centered.
    // @Units: cm/s/s
    // @Range: 25 250
    // @Increment: 1
    // @User: Advanced
    AP_GROUPINFO("BRK_ACCEL", 4, AC_Loiter, _brake_accel_max_cmss, LOITER_BRAKE_ACCEL_DEFAULT_CMSS),

    // @Param: BRK_JERK
    // @DisplayName: Loiter braking jerk
    // @Description: Loiter braking jerk in cm/s/s/s. Higher values will remove braking faster if the pilot moves the sticks during a braking maneuver.
    // @Units: cm/s/s/s
    // @Range: 500 5000
    // @Increment: 1
    // @User: Advanced
    AP_GROUPINFO("BRK_JERK", 5, AC_Loiter, _brake_jerk_max_cmsss, LOITER_BRAKE_JERK_DEFAULT_CMSSS),

    // @Param: BRK_DELAY
    // @DisplayName: Loiter brake start delay (in seconds)
    // @Description: Loiter brake start delay (in seconds)
    // @Units: s
    // @Range: 0 2
    // @Increment: 0.1
    // @User: Advanced
    AP_GROUPINFO("BRK_DELAY",  6, AC_Loiter, _brake_delay_s, LOITER_BRAKE_START_DELAY_DEFAULT_S),

    AP_GROUPEND





AC_Loiter::AC_Loiter(const AP_AHRS_View& ahrs, AC_PosControl& pos_control, const AC_AttitudeControl& attitude_control) :




    AP_Param::setup_object_defaults(this, var_info);




void AC_Loiter::init_target_cm(const Vector2f& position_ne_cm)







void AC_Loiter::init_target_m(const Vector2f& position_ne_m)






















void AC_Loiter::init_target()



















void AC_Loiter::soften_for_landing()






void AC_Loiter::set_pilot_desired_acceleration_cd(float euler_roll_angle_cd, float euler_pitch_angle_cd)








void AC_Loiter::set_pilot_desired_acceleration_rad(float euler_roll_angle_rad, float euler_pitch_angle_rad)






























void AC_Loiter::get_stopping_point_NE_cm(Vector2f& stopping_point_ne_cm) const











void AC_Loiter::get_stopping_point_NE_m(Vector2f& stopping_point_ne_m) const











float AC_Loiter::get_angle_max_cd() const








float AC_Loiter::get_angle_max_rad() const











void AC_Loiter::update(bool avoidance_on)










void AC_Loiter::set_speed_max_NE_cms(float speed_max_ne_cms)






void AC_Loiter::set_speed_max_NE_ms(float speed_max_ne_ms)







void AC_Loiter::sanity_check_params()












void AC_Loiter::calc_desired_velocity(bool avoidance_on)
